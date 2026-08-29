"""Proving the test segment cannot reach the model.

The split arithmetic is checked next door, on dates. These check the thing dates
cannot: that no value from the test segment influences what gets fitted.

Three independent probes, because each catches a different route in:

- a **fitted processor** estimating its parameters over more than the training
  range, which is the leak the fit window exists to prevent and which nothing
  today would notice, since none of the production processors fit anything;
- **test labels** reaching the training loss;
- **test features** being read during fitting or preprocessing.

The last two are separate on purpose. Changing only the labels proves nothing
about the features: a `prepare()` call with the wrong segment would pull test
feature rows in while leaving every label untouched.
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
from app.research.lgbm import CancellableLGBModel, model_checksums
from app.research.model_definition import (
    DEFAULT_SEED,
    ModelResearchDefinition,
    resolve_model_params,
)
from app.research.splits import build_split_plan, propose_split
from tests.test_execution_spec import _stored

pytest.importorskip("qlib")

SECURITIES = [f"{index:08d}-0000-0000-0000-000000000000" for index in range(40)]

SMALL_SET = FeatureSet(
    name="leak_probe_v1",
    version="1",
    description="A few price features, for leakage probes.",
    features=(
        _spec("ROC5", "Ref($close, 5)/$close"),
        _spec("MA10", "Mean($close, 10)/$close"),
        _spec("STD10", "Std($close, 10)/$close"),
    ),
)


@pytest.fixture(scope="module")
def market() -> tuple[list[date], list[date]]:
    start = date(2025, 1, 6)
    sessions = [
        start + timedelta(days=offset)
        for offset in range(30 * 7)
        if (start + timedelta(days=offset)).weekday() < 5
    ]
    return sessions, [item for item in sessions if item.weekday() == 4]


def _write_bundle(root, sessions, *, corrupt_from: date | None = None) -> None:
    rng = np.random.default_rng(11)
    frames = []
    for index, security in enumerate(SECURITIES):
        steps = rng.normal(loc=0.001 * (index % 7), scale=0.01, size=len(sessions))
        close = 100.0 * np.exp(np.cumsum(steps))
        if corrupt_from is not None:
            # A value no ordinary price path reaches, from `corrupt_from`
            # onwards. If any of it is read while fitting, the model changes.
            mask = np.array([day >= corrupt_from for day in sessions])
            close = np.where(mask, close * 1000.0, close)
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


def _spec_for(market, monkeypatch):
    sessions, weekly = market
    monkeypatch.setattr(
        "app.research.execution_spec.get_feature_set",
        lambda name: SMALL_SET,
    )
    definition = ModelResearchDefinition(
        data_snapshot_id=__import__("uuid").uuid4(),
        feature_set=SMALL_SET,
        split=build_split_plan(
            weekly_observations=weekly, calendar=sessions, **propose_split(weekly)
        ),
        stock_pool_policy_fingerprint="leak",
        model_params=resolve_model_params({"min_data_in_leaf": 20}, seed=DEFAULT_SEED),
    )
    return compile_execution_spec(_stored(definition))


def _train(assembled) -> str:
    params = dict(resolve_model_params({"min_data_in_leaf": 20}, seed=DEFAULT_SEED))
    params.pop("num_boost_round")
    params.pop("early_stopping_rounds")
    model = CancellableLGBModel(
        loss=params.pop("objective"),
        early_stopping_rounds=20,
        num_boost_round=40,
        num_threads=1,
        **params,
    )
    model.fit(assembled.dataset, verbose_eval=0)
    return model_checksums(model.model.model_to_string())[0]


def _assemble(bundle, spec, market):
    sessions, weekly = market
    return build_dataset(
        bundle_path=bundle,
        spec=spec,
        universe={day: set(SECURITIES) for day in weekly},
        weekly_observations=weekly,
        calendar=sessions,
    )


# --------------------------------------------------------------------------
# 1. A fitted processor may only see the training range
# --------------------------------------------------------------------------


def test_a_fitted_processor_only_uses_training_rows(market, tmp_path, monkeypatch) -> None:
    """The fit window is a contract with no consumer today.

    Nothing in the production pipeline fits anything, so nothing would notice if
    the window were wrong — until someone adds a linear model, which needs a
    fitted normaliser, and it silently estimates over the whole range.

    The assertion is on the parameters the processor ended up with, not on the
    frame it was handed. `ZScoreNorm.fit` slices internally
    (`fetch_df_by_index(df, slice(fit_start_time, fit_end_time))`), so it is
    given everything by design and intercepting its argument would only prove
    that. What matters is which rows the statistics came out of.
    """
    from qlib.contrib.data.handler import check_transform_proc
    from qlib.data.dataset.processor import ZScoreNorm

    sessions, weekly = market
    bundle = tmp_path / "bundle"
    _write_bundle(bundle, sessions)
    spec = _spec_for(market, monkeypatch)

    processors = check_transform_proc(
        [{"class": "ZScoreNorm", "kwargs": {"fields_group": "feature"}}],
        spec.fit_start,
        spec.fit_end,
    )
    # The window has to reach the processor, or the rest proves nothing.
    assert processors[0]["kwargs"]["fit_start_time"] == spec.fit_start
    assert processors[0]["kwargs"]["fit_end_time"] == spec.fit_end

    assembled = _assemble(bundle, spec, market)
    frame = assembled.handler.fetch(col_set="feature", data_key="raw")
    processor = ZScoreNorm(
        fit_start_time=spec.fit_start, fit_end_time=spec.fit_end, fields_group=None
    )
    processor.fit(frame)

    stamps = frame.index.get_level_values("datetime")
    train_only = frame.loc[
        (stamps >= pd.Timestamp(spec.fit_start)) & (stamps <= pd.Timestamp(spec.fit_end))
    ]

    assert np.allclose(processor.mean_train, np.nanmean(train_only.values, axis=0), equal_nan=True)
    # And the whole-range statistics are genuinely different, so the assertion
    # above is discriminating rather than trivially true.
    assert not np.allclose(
        processor.mean_train, np.nanmean(frame.values, axis=0), equal_nan=True
    )


# --------------------------------------------------------------------------
# 2 and 3. Sentinels
# --------------------------------------------------------------------------


def test_corrupting_test_labels_does_not_change_the_model(market, tmp_path, monkeypatch) -> None:
    """Any path that lets a test label into the training loss fails this."""
    sessions, weekly = market
    bundle = tmp_path / "bundle"
    _write_bundle(bundle, sessions)
    spec = _spec_for(market, monkeypatch)

    baseline = _assemble(bundle, spec, market)
    reference = _train(baseline)

    poisoned = _assemble(bundle, spec, market)
    loader = poisoned.handler.data_loader
    frame = loader._config["label"].copy()
    after_test = frame.index.get_level_values("datetime") >= pd.Timestamp(spec.split.test.start)
    frame.loc[after_test, LABEL_COLUMN] = 999.0
    loader._config["label"] = frame
    poisoned.handler.setup_data(enable_cache=False)

    assert _train(poisoned) == reference


def test_corrupting_test_features_does_not_change_the_model(market, tmp_path, monkeypatch) -> None:
    """The half a label-only sentinel cannot see.

    A `prepare()` call naming the wrong segment would pull test feature rows
    into fitting while leaving every label untouched, and the label sentinel
    would pass.
    """
    sessions, weekly = market
    spec = _spec_for(market, monkeypatch)

    clean = tmp_path / "clean"
    _write_bundle(clean, sessions)
    reference = _train(_assemble(clean, spec, market))

    corrupt = tmp_path / "corrupt"
    _write_bundle(corrupt, sessions, corrupt_from=spec.split.test.start)

    assert _train(_assemble(corrupt, spec, market)) == reference


def test_the_sentinels_can_actually_fail(market, tmp_path, monkeypatch) -> None:
    """A sentinel that cannot fail proves nothing.

    Corrupting from the *train* segment instead must change the model — if it
    does not, the probe is inert and the two tests above are worthless.
    """
    sessions, weekly = market
    spec = _spec_for(market, monkeypatch)

    clean = tmp_path / "clean"
    _write_bundle(clean, sessions)
    reference = _train(_assemble(clean, spec, market))

    corrupt = tmp_path / "corrupt"
    _write_bundle(corrupt, sessions, corrupt_from=spec.split.train.start)

    assert _train(_assemble(corrupt, spec, market)) != reference
