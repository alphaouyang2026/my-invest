"""Assembling the training matrix, against a real Qlib bundle.

Written on a synthetic bundle rather than a fake reader because the properties
under test are properties of the read: which rows survive the universe filter,
what the segment boundaries actually slice, and whether a label column can reach
the feature matrix. A fake would only restate the assumptions.

The universe filter is the one worth being careful about. A security admitted to
the pool in week 10 must not contribute a week 2 training row: by the time that
shows up in a metric the model has already been fitted on rows it was never
entitled to.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.research.bundle import write_native_bundle
from app.research.dataset import LABEL_COLUMN, build_dataset
from app.research.execution_spec import compile_execution_spec
from app.research.feature_sets import FeatureSet, _spec
from app.research.model_definition import (
    DEFAULT_SEED,
    ModelResearchDefinition,
    resolve_model_params,
)
from app.research.splits import build_split_plan, propose_split
from tests.test_execution_spec import _stored

pytest.importorskip("qlib")

SECURITIES = [f"{index:08d}-0000-0000-0000-000000000000" for index in range(12)]


@pytest.fixture(scope="module")
def market() -> tuple[list[date], list[date]]:
    start = date(2025, 1, 6)
    sessions = [
        start + timedelta(days=offset)
        for offset in range(30 * 7)
        if (start + timedelta(days=offset)).weekday() < 5
    ]
    return sessions, [item for item in sessions if item.weekday() == 4]


@pytest.fixture(scope="module")
def bundle(tmp_path_factory, market):
    """A tiny bundle with a real price path per security."""
    sessions, _ = market
    root = tmp_path_factory.mktemp("bundle")
    rng = np.random.default_rng(7)
    frames = []
    for index, security in enumerate(SECURITIES):
        steps = rng.normal(loc=0.001 * (index + 1), scale=0.01, size=len(sessions))
        close = 100.0 * np.exp(np.cumsum(steps))
        frames.append(
            (
                security,
                pd.DataFrame(
                    {
                        "open": close * 0.99,
                        "high": close * 1.02,
                        "low": close * 0.97,
                        "close": close,
                        "volume": np.full(len(sessions), 1_000.0),
                        "vwap": close,
                        "factor": np.ones(len(sessions)),
                    },
                    index=pd.Index(sessions),
                ).astype("float32"),
            )
        )
    write_native_bundle(root, calendar=sessions, features=frames)
    return root


#: A handful of columns rather than Alpha158: the assembly logic is identical
#: and a 158-column read makes every assertion slower without making any of them
#: stronger.
SMALL_SET = FeatureSet(
    name="small_test_v1",
    version="1",
    description="A few price features, for exercising dataset assembly.",
    features=(
        _spec("ROC5", "Ref($close, 5)/$close"),
        _spec("MA10", "Mean($close, 10)/$close"),
        _spec("STD10", "Std($close, 10)/$close"),
        _spec("VWAP0", "$vwap/$close"),
    ),
)


@pytest.fixture()
def spec(market, monkeypatch):
    sessions, weekly = market
    monkeypatch.setattr(
        "app.research.execution_spec.get_feature_set",
        lambda name: SMALL_SET if name == SMALL_SET.name else pytest.fail(name),
    )
    definition = ModelResearchDefinition(
        data_snapshot_id=__import__("uuid").uuid4(),
        feature_set=SMALL_SET,
        split=build_split_plan(
            weekly_observations=weekly, calendar=sessions, **propose_split(weekly)
        ),
        stock_pool_policy_fingerprint="test",
        model_params=resolve_model_params(None, seed=DEFAULT_SEED),
    )
    return compile_execution_spec(_stored(definition))


def _universe(weekly, members=None) -> dict[date, set[str]]:
    return {day: set(members or SECURITIES) for day in weekly}


def test_the_assembled_frame_carries_features_and_labels(bundle, market, spec) -> None:
    sessions, weekly = market

    assembled = build_dataset(
        bundle_path=bundle,
        spec=spec,
        universe=_universe(weekly),
        weekly_observations=weekly,
        calendar=sessions,
    )
    train = assembled.dataset.prepare("train", col_set=["feature", "label"])

    assert list(train["feature"].columns) == list(SMALL_SET.column_names)
    assert list(train["label"].columns) == [LABEL_COLUMN]
    assert len(train) > 0


def test_a_security_outside_the_frozen_universe_contributes_no_row(bundle, market, spec) -> None:
    """Point-in-time eligibility, enforced before training rather than after."""
    sessions, weekly = market
    admitted = SECURITIES[:6]

    assembled = build_dataset(
        bundle_path=bundle,
        spec=spec,
        universe=_universe(weekly, admitted),
        weekly_observations=weekly,
        calendar=sessions,
    )
    train = assembled.dataset.prepare("train", col_set=["feature"])

    assert set(train.index.get_level_values("instrument")) <= set(admitted)


def test_a_security_admitted_later_contributes_nothing_earlier(bundle, market, spec) -> None:
    sessions, weekly = market
    late = SECURITIES[-1]
    universe = {
        day: set(SECURITIES if index >= 10 else SECURITIES[:-1])
        for index, day in enumerate(weekly)
    }

    assembled = build_dataset(
        bundle_path=bundle,
        spec=spec,
        universe=universe,
        weekly_observations=weekly,
        calendar=sessions,
    )
    frame = assembled.dataset.prepare("train", col_set=["feature"])
    appearances = frame.index.get_level_values("instrument") == late

    assert not appearances[: len(frame) // 2].any() or frame.index[appearances][0][0] >= pd.Timestamp(
        weekly[10]
    )


def test_the_segments_do_not_overlap_and_respect_the_boundaries(bundle, market, spec) -> None:
    sessions, weekly = market

    assembled = build_dataset(
        bundle_path=bundle,
        spec=spec,
        universe=_universe(weekly),
        weekly_observations=weekly,
        calendar=sessions,
    )
    windows = {
        name: assembled.dataset.prepare(name, col_set=["feature"]).index.get_level_values("datetime")
        for name in ("train", "valid", "test")
    }

    assert windows["train"].max() <= pd.Timestamp(spec.split.train.end)
    assert windows["valid"].min() >= pd.Timestamp(spec.split.valid.start)
    assert windows["test"].min() >= pd.Timestamp(spec.split.test.start)
    assert windows["train"].max() < windows["valid"].min() < windows["test"].min()


def test_no_label_column_reaches_the_feature_matrix(bundle, market, spec) -> None:
    """The cheapest leak to introduce and the hardest to notice afterwards."""
    sessions, weekly = market

    assembled = build_dataset(
        bundle_path=bundle,
        spec=spec,
        universe=_universe(weekly),
        weekly_observations=weekly,
        calendar=sessions,
    )
    features = assembled.dataset.prepare("train", col_set=["feature"])

    assert LABEL_COLUMN not in features.columns
    assert not any("label" in str(column).lower() for column in features.columns)


def test_missing_rates_are_reported_per_segment_and_column(bundle, market, spec) -> None:
    """Nothing is imputed, so the missing structure has to be visible."""
    sessions, weekly = market

    assembled = build_dataset(
        bundle_path=bundle,
        spec=spec,
        universe=_universe(weekly),
        weekly_observations=weekly,
        calendar=sessions,
    )

    assert set(assembled.missing_rate["segment"]) == {"train", "valid", "test"}
    assert set(assembled.missing_rate["feature_name"]) == set(SMALL_SET.column_names)
    assert (assembled.missing_rate["missing_rate"] >= 0).all()


def test_the_handler_declares_the_train_segment_as_its_fit_window(bundle, market, spec) -> None:
    """No processor fits anything today; the contract still has to be right.

    The first fitted processor someone adds — a linear model needs one — would
    otherwise estimate its parameters over the whole range without any error.
    """
    sessions, weekly = market

    assembled = build_dataset(
        bundle_path=bundle,
        spec=spec,
        universe=_universe(weekly),
        weekly_observations=weekly,
        calendar=sessions,
    )

    assert assembled.fit_window == (spec.split.train.start, spec.split.train.end)
