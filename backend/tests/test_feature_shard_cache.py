"""The feature cache, tested where a run cannot see it fail.

Reading short is the failure this file exists to prevent. The cache is its own
source of truth for which rows exist, so a segment missing days comes back small
rather than empty, and a small segment is indistinguishable from a genuinely
small one. Most of what follows is an assertion about that.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.experiments.feature_shard_cache import (
    DateSpan,
    FeatureCacheError,
    FeatureCacheSpec,
    ShardedDataset,
    assemble_sharded_dataset,
    cache_identity,
    restrict_to_universe,
)

SNAPSHOT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _fixture(monkeypatch, tmp_path, *, columns=1, raw_override=None, cache="cache", hostile=False):
    """A tiny provider whose features and closes are known exactly."""
    days = [date(2025, 1, day) for day in range(2, 18) if date(2025, 1, day).weekday() < 5]
    future = list(pd.bdate_range(days[-1] + timedelta(days=1), periods=8).date)
    instruments = [str(SNAPSHOT_ID), "22222222-2222-2222-2222-222222222222"]
    index = pd.MultiIndex.from_product(
        [instruments, pd.to_datetime(days)], names=["instrument", "datetime"]
    )
    expressions = tuple(f"Ref($close, {n + 1})" for n in range(columns))
    names = tuple(f"CLOSE_LAG{n + 1}" for n in range(columns))
    if raw_override is not None:
        raw = raw_override
    else:
        values = {
            expression: np.arange(len(index), dtype="float64") + offset
            for offset, expression in enumerate(expressions)
        }
        # Different price *paths* per security, so a cross-section has something
        # to rank. Identical paths made CSRankNorm's only test a table of ties,
        # and merely scaling one leaves the forward return unchanged.
        closes = np.concatenate([
            100.0 + np.arange(len(days)) * (1.0 + number) + number * 7.0
            for number in range(len(instruments))
        ])
        raw = pd.DataFrame({**values, "$close": closes}, index=index)
        if hostile:
            # InfToNaN has nothing to convert unless something is infinite, and
            # a NaN must survive the round trip rather than being filled.
            first = raw.columns[0]
            raw.iloc[1, raw.columns.get_loc(first)] = np.inf
            raw.iloc[3, raw.columns.get_loc(first)] = -np.inf
            raw.iloc[5, raw.columns.get_loc(first)] = np.nan
            # A zero close makes the forward return infinite, which is the label
            # side of the same hazard: an infinite label reads as valid to the
            # evaluation's `notna()` and would be ranked as an extreme.
            raw.iloc[7, raw.columns.get_loc("$close")] = 0.0

    def read(path, *, instruments, **kwargs):
        wanted = list(instruments)
        return raw.loc[raw.index.get_level_values("instrument").isin(wanted)]

    monkeypatch.setattr("app.experiments.feature_shard_cache.read_features", read)
    feature_set = SimpleNamespace(
        name="test_set", expressions=expressions, column_names=names,
        definition_checksum="checksum",
    )
    provider = SimpleNamespace(
        path="unused",
        manifest=SimpleNamespace(
            coverage_start=days[0], coverage_end=future[-1],
            calendar=tuple([*days, *future]), logical_checksum="provider-checksum",
        ),
    )
    snapshot = SimpleNamespace(
        id=SNAPSHOT_ID, version=1, bar_publish_sequence=7, coverage_end=days[-1]
    )
    spec = FeatureCacheSpec(
        span=DateSpan(days[0], days[-1]),
        label_horizon=5,
        feature_batch_size=1,  # Force more than one shard even at this size.
    )
    universe = {day: set(instruments) for day in days}
    dataset = assemble_sharded_dataset(
        provider, feature_set, spec, universe,
        snapshot=snapshot, policy_fingerprint="policy", cache_root=tmp_path / cache,
        required_days=days,
    )
    return SimpleNamespace(
        dataset=dataset, days=days, instruments=instruments, raw=raw, snapshot=snapshot,
        feature_set=feature_set, spec=spec, universe=universe, provider=provider,
    )


def test_the_shard_cache_reproduces_the_qlib_handler_pipeline(monkeypatch, tmp_path) -> None:
    """The processors are hand-written here, so their equivalence must be asserted.

    `InfToNaN`, `DropnaLabel` and `CSRankNorm` used to be applied by Qlib through
    `DataHandlerLP`. This applies the first while writing a shard, the third while
    merging labels and the second while loading a segment. Here the Qlib pipeline
    the module no longer uses is built, and required to give the same numbers.
    """
    from qlib.contrib.data.handler import check_transform_proc
    from qlib.data.dataset import DatasetH
    from qlib.data.dataset.handler import DataHandlerLP
    from qlib.data.dataset.loader import StaticDataLoader

    from app.research.processors import InfToNaN

    fixture = _fixture(monkeypatch, tmp_path, columns=3, hostile=True)
    spec, feature_set = fixture.spec, fixture.feature_set

    features = restrict_to_universe(fixture.raw, fixture.universe)[
        list(feature_set.expressions)
    ].copy()
    features.columns = list(feature_set.column_names)
    features = features.swaplevel().sort_index()
    features.index = features.index.set_names(["datetime", "instrument"])
    close = fixture.raw["$close"].groupby(level="instrument")
    forward = close.shift(-(spec.label_horizon + 1)) / close.shift(-1) - 1
    forward = forward.swaplevel().sort_index()
    forward.index = forward.index.set_names(["datetime", "instrument"])
    label_frame = forward.reindex(features.index).rename("LABEL0").to_frame()
    handler = DataHandlerLP(
        instruments=None,
        data_loader=StaticDataLoader(config={"feature": features, "label": label_frame}),
        infer_processors=check_transform_proc(
            [InfToNaN(fields_group="feature")], spec.span.start, spec.span.end
        ),
        learn_processors=check_transform_proc(
            [{"class": "DropnaLabel"},
             {"class": "CSRankNorm", "kwargs": {"fields_group": "label"}}],
            spec.span.start, spec.span.end,
        ),
        process_type=DataHandlerLP.PTYPE_A,
    )
    whole = DateSpan(fixture.days[0], fixture.days[-1])
    reference = DatasetH(
        handler=handler, segments={"all": (whole.start, whole.end)}
    ).prepare("all", col_set=["feature", "label"], data_key=DataHandlerLP.DK_L)
    expected_features = reference["feature"].sort_index()
    expected_labels = reference["label"].iloc[:, 0].sort_index()

    segment = fixture.dataset.load_segment(whole, learning=True)
    actual = pd.DataFrame(
        segment.features, index=segment.index, columns=list(segment.feature_names)
    ).sort_index()

    # The fixture must actually contain what the processors are here to handle.
    assert np.isinf(fixture.raw.iloc[:, 0]).any(), "no infinity for InfToNaN to convert"
    assert (fixture.raw["$close"] == 0).any(), "no divide-by-zero to make a label infinite"
    assert expected_labels.nunique() > 1, "a table of ties cannot test CSRankNorm"

    assert list(actual.index) == list(expected_features.index)
    np.testing.assert_allclose(
        actual.to_numpy(), expected_features.to_numpy().astype("float32"),
        rtol=1e-6, equal_nan=True,
    )
    # Infinities became NaN on both sides rather than surviving into training.
    assert not np.isinf(actual.to_numpy()).any()
    assert actual.isna().to_numpy().sum() == expected_features.isna().to_numpy().sum() > 0
    np.testing.assert_allclose(
        pd.Series(segment.labels, index=segment.index).sort_index().to_numpy(),
        expected_labels.to_numpy(),
        rtol=1e-9,
    )


def test_the_cache_is_reused_without_touching_qlib_again(monkeypatch, tmp_path) -> None:
    """The reason a search can call the run entry point eighty times."""
    fixture = _fixture(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        "app.experiments.feature_shard_cache.read_features",
        lambda *a, **k: calls.append(1) or fixture.raw,
    )
    again = assemble_sharded_dataset(
        fixture.provider, fixture.feature_set, fixture.spec, fixture.universe,
        snapshot=fixture.snapshot, policy_fingerprint="policy", cache_root=tmp_path / "cache",
        required_days=fixture.days,
    )
    assert calls == []
    assert again.root == fixture.dataset.root


def test_the_prediction_only_tail_survives_the_cache(monkeypatch, tmp_path) -> None:
    fixture = _fixture(monkeypatch, tmp_path)
    labels = fixture.dataset.raw_labels()

    tail = labels[labels["observation_date"] == fixture.days[-1]]
    assert set(tail["label_reason"]) == {"label_not_mature"}
    # Each security walks its own price path, so each has its own forward return.
    first = labels[labels["observation_date"] == fixture.days[0]].set_index("instrument_id")
    closes = fixture.raw["$close"]
    for instrument in fixture.instruments:
        path = closes.xs(instrument, level="instrument").to_numpy()
        assert first.loc[instrument, "label"] == pytest.approx(path[6] / path[1] - 1)

    # An inference segment keeps the immature tail; a learning one drops it.
    late = DateSpan(fixture.days[8], fixture.days[-1])
    inferred = fixture.dataset.load_segment(late, learning=False)
    assert pd.Timestamp(fixture.days[-1]) in inferred.index.get_level_values("datetime")
    learned = fixture.dataset.load_segment(late, learning=True)
    assert pd.Timestamp(fixture.days[-1]) not in learned.index.get_level_values("datetime")


def test_test_values_cannot_change_the_prepared_training_matrix(monkeypatch, tmp_path) -> None:
    """Perturbing the late window must not move one number in the early one."""
    baseline = _fixture(monkeypatch, tmp_path, cache="a")
    early = DateSpan(baseline.days[0], baseline.days[2])
    train = baseline.dataset.load_segment(early, learning=True)

    perturbed = baseline.raw.copy()
    late = pd.to_datetime([day for day in baseline.days if day >= baseline.days[8]])
    mask = perturbed.index.get_level_values("datetime").isin(late)
    perturbed.loc[mask, "Ref($close, 1)"] *= 1000.0
    other = _fixture(monkeypatch, tmp_path, raw_override=perturbed, cache="b")
    perturbed_train = other.dataset.load_segment(early, learning=True)

    assert list(train.index) == list(perturbed_train.index)
    np.testing.assert_array_equal(train.features, perturbed_train.features)
    np.testing.assert_array_equal(train.labels, perturbed_train.labels)


def test_the_pool_filter_does_not_truncate_the_forward_label(monkeypatch, tmp_path) -> None:
    """A label reaches past the days its security is in the pool.

    Restricting features to the pool is what keeps the panel small; doing the same
    to the close series would cut every label short near the boundary.
    """
    days = list(pd.bdate_range("2020-01-01", periods=12).date)
    index = pd.MultiIndex.from_product(
        [["stock"], pd.to_datetime(days)], names=["instrument", "datetime"]
    )
    raw = pd.DataFrame({"$close": np.arange(100.0, 112.0)}, index=index)
    monkeypatch.setattr(
        "app.experiments.feature_shard_cache.read_features", lambda *a, **k: raw
    )
    feature_set = SimpleNamespace(
        name="one", expressions=("$close",), column_names=("CLOSE0",), definition_checksum="c"
    )
    provider = SimpleNamespace(
        path=tmp_path,
        manifest=SimpleNamespace(
            calendar=tuple(days), coverage_start=days[0], coverage_end=days[-1],
            logical_checksum="p",
        ),
    )
    snapshot = SimpleNamespace(
        id=SNAPSHOT_ID, version=1, bar_publish_sequence=1, coverage_end=days[-1]
    )
    spec = FeatureCacheSpec(span=DateSpan(days[0], days[-1]), label_horizon=5)

    dataset = assemble_sharded_dataset(
        provider, feature_set, spec, {days[0]: {"stock"}},
        snapshot=snapshot, policy_fingerprint="p", cache_root=tmp_path / "cache",
        required_days=[days[0]],
    )
    labels = dataset.raw_labels()

    assert len(labels) == 1  # Only the one pooled day carries a feature row.
    assert labels.iloc[0]["label"] == pytest.approx(106 / 101 - 1)


# ---------------------------------------------------------------------------
# The bounded reader
# ---------------------------------------------------------------------------


def _shard_files(root, names, batches, labels):
    for number, rows in enumerate(batches, 1):
        folder = root / f"batch-{number:04d}"
        folder.mkdir(parents=True)
        frame = pd.DataFrame(rows, columns=["datetime", "instrument", *names])
        frame["datetime"] = pd.to_datetime(frame["datetime"])
        frame.to_parquet(folder / "features.parquet")
    labels.to_parquet(root / "labels.parquet")


def test_a_segment_is_scattered_into_one_matrix_in_label_order(tmp_path) -> None:
    """Rows arrive shard by shard and land at their final position directly.

    Concatenating per-shard frames would hold the panel twice at the join, which
    is the cost this reader exists to avoid.
    """
    root = tmp_path / "alpha360"
    names = ["F0", "F1"]
    rows = [
        ("2020-01-02", "a", 1.0, 2.0),
        ("2020-01-03", "a", 3.0, np.nan),
        ("2020-01-02", "b", 4.0, 5.0),
        ("2020-01-03", "b", 6.0, 7.0),
    ]
    labels = pd.DataFrame({
        "datetime": pd.to_datetime([row[0] for row in rows]),
        "instrument": [row[1] for row in rows],
        "raw_h5": [.1, .2, .3, np.nan],
        "learn_h5": [-1.73, -1.73, 1.73, np.nan],
    })
    _shard_files(root, names, [rows[:2], rows[2:]], labels)
    data = ShardedDataset(
        root, tuple(names), 5, 2, frozenset(), DateSpan(date(2020, 1, 2), date(2020, 1, 3))
    )

    segment = data.load_segment(DateSpan(date(2020, 1, 2), date(2020, 1, 3)), learning=True)

    assert segment.index.tolist() == [
        (pd.Timestamp("2020-01-02"), "a"),
        (pd.Timestamp("2020-01-02"), "b"),
        (pd.Timestamp("2020-01-03"), "a"),
    ]
    np.testing.assert_allclose(segment.features, [[1, 2], [4, 5], [3, np.nan]], equal_nan=True)
    np.testing.assert_allclose(segment.labels, [-1.73, 1.73, -1.73])


def test_a_duplicated_shard_row_is_refused_rather_than_overwritten(tmp_path) -> None:
    root = tmp_path / "data"
    labels = pd.DataFrame({
        "datetime": pd.to_datetime(["2020-01-02"]), "instrument": ["a"],
        "raw_h5": [.1], "learn_h5": [0.0],
    })
    _shard_files(root, ["F0"], [[("2020-01-02", "a", 1.0), ("2020-01-02", "a", 1.0)]], labels)
    data = ShardedDataset(
        root, ("F0",), 5, 2, frozenset(), DateSpan(date(2020, 1, 2), date(2020, 1, 2))
    )

    with pytest.raises(FeatureCacheError, match="Duplicate feature row"):
        data.load_segment(DateSpan(date(2020, 1, 2), date(2020, 1, 2)), learning=True)


def test_a_run_needing_a_day_the_cache_lacks_is_refused(monkeypatch, tmp_path) -> None:
    """Presence is checked before a reader exists, not asked of one afterwards.

    A cache built for one set of days and read for another loses interior days,
    not boundary ones, and a short panel trains without complaint. Comparing the
    two sets is therefore a precondition of getting a reader at all.
    """
    fixture = _fixture(monkeypatch, tmp_path)

    with pytest.raises(FeatureCacheError, match="missing 1 of the"):
        assemble_sharded_dataset(
            fixture.provider, fixture.feature_set, fixture.spec, fixture.universe,
            snapshot=fixture.snapshot, policy_fingerprint="policy",
            cache_root=tmp_path / "cache",
            required_days=[*fixture.days, date(2030, 1, 2)],
        )


def test_a_day_the_provider_only_half_answered_is_never_sealed(monkeypatch, tmp_path) -> None:
    """A day can be present and still be short, which is the harder failure.

    Features and labels are derived from the same filtered frame, so a provider
    answering for part of a batch shortens both together and the row-level checks
    see nothing wrong. Only the stock pool knows how many members a day has.
    """
    days = list(pd.bdate_range("2020-01-01", periods=8).date)
    instruments = [f"s{number}" for number in range(10)]
    index = pd.MultiIndex.from_product(
        [instruments, pd.to_datetime(days)], names=["instrument", "datetime"]
    )
    raw = pd.DataFrame(
        {
            "$close": np.tile(np.arange(100.0, 100.0 + len(days)), len(instruments)),
            "F": np.arange(len(index), dtype="float64"),
        },
        index=index,
    )
    starved = raw.drop(index=[(s, pd.Timestamp(days[4])) for s in instruments[3:]])
    monkeypatch.setattr(
        "app.experiments.feature_shard_cache.read_features",
        lambda path, *, instruments, **kwargs: starved.loc[
            starved.index.get_level_values("instrument").isin(list(instruments))
        ],
    )
    feature_set = SimpleNamespace(
        name="f", expressions=("F",), column_names=("F0",), definition_checksum="c"
    )
    provider = SimpleNamespace(
        path="x",
        manifest=SimpleNamespace(
            coverage_start=days[0], coverage_end=days[-1],
            calendar=tuple(days), logical_checksum="p",
        ),
    )
    snapshot = SimpleNamespace(
        id=SNAPSHOT_ID, version=1, bar_publish_sequence=1, coverage_end=days[-1]
    )

    with pytest.raises(FeatureCacheError, match=f"first {days[4]} with 3 of 10"):
        assemble_sharded_dataset(
            provider, feature_set, FeatureCacheSpec(DateSpan(days[0], days[-1]), 2),
            {day: set(instruments) for day in days},
            snapshot=snapshot, policy_fingerprint="p", cache_root=tmp_path / "cache",
            required_days=days,
        )


def test_a_day_the_provider_cannot_answer_at_all_is_never_sealed(monkeypatch, tmp_path) -> None:
    """The same check, at the other extreme: a day with no rows at all.

    Sealing a short cache would move the failure to whoever reads it next.
    """
    days = list(pd.bdate_range("2020-01-01", periods=6).date)
    index = pd.MultiIndex.from_product(
        [["stock"], pd.to_datetime(days[:3])], names=["instrument", "datetime"]
    )
    raw = pd.DataFrame({"$close": np.arange(100.0, 103.0)}, index=index)
    monkeypatch.setattr(
        "app.experiments.feature_shard_cache.read_features", lambda *a, **k: raw
    )
    feature_set = SimpleNamespace(
        name="one", expressions=("$close",), column_names=("CLOSE0",), definition_checksum="c"
    )
    provider = SimpleNamespace(
        path=tmp_path,
        manifest=SimpleNamespace(
            calendar=tuple(days), coverage_start=days[0], coverage_end=days[-1],
            logical_checksum="p",
        ),
    )
    snapshot = SimpleNamespace(
        id=SNAPSHOT_ID, version=1, bar_publish_sequence=1, coverage_end=days[-1]
    )
    # The pool names six days; the provider can only answer for three.
    universe = {day: {"stock"} for day in days}

    with pytest.raises(FeatureCacheError, match="with 0 of 1"):
        assemble_sharded_dataset(
            provider, feature_set, FeatureCacheSpec(DateSpan(days[0], days[-1]), 1), universe,
            snapshot=snapshot, policy_fingerprint="p", cache_root=tmp_path / "cache",
            required_days=days,
        )


# ---------------------------------------------------------------------------
# The identity has to cover everything the cache contains
# ---------------------------------------------------------------------------


def _identity_spec(**changes):
    base = dict(span=DateSpan(date(2025, 1, 1), date(2025, 9, 26)), label_horizon=5)
    return FeatureCacheSpec(**{**base, **changes})


def _identity_of(spec):
    provider = SimpleNamespace(manifest=SimpleNamespace(logical_checksum="p"))
    feature_set = SimpleNamespace(name="f", definition_checksum="c")
    snapshot = SimpleNamespace(id=SNAPSHOT_ID, version=1, bar_publish_sequence=1)
    return cache_identity(provider, feature_set, spec, snapshot, "policy")


def test_the_spec_holds_only_what_changes_the_cached_rows() -> None:
    """The bug this replaced: fold geometry moved the contents without moving the
    identity, so a later run read a short training set and did not fail.

    A separate type from the run's configuration is what makes that hard to
    reintroduce -- nothing about how folds fall inside the span can be said here,
    which is exactly why the span is sufficient.
    """
    assert set(FeatureCacheSpec.__dataclass_fields__) == {
        "span", "label_horizon", "feature_batch_size", "qlib_kernels", "scan_batch_rows",
    }


def test_anything_that_changes_the_cached_rows_changes_the_identity() -> None:
    baseline = _identity_of(_identity_spec())
    assert _identity_of(_identity_spec(label_horizon=20)) != baseline
    assert _identity_of(
        _identity_spec(span=DateSpan(date(2025, 2, 3), date(2025, 9, 26)))
    ) != baseline
    assert _identity_of(
        _identity_spec(span=DateSpan(date(2025, 1, 1), date(2025, 9, 19)))
    ) != baseline


def test_batch_sizes_do_not_invalidate_a_cache_they_cannot_change() -> None:
    """They divide the work; the shard plan guards a half-built cache."""
    baseline = _identity_of(_identity_spec())
    assert _identity_of(_identity_spec(feature_batch_size=32)) == baseline
    assert _identity_of(_identity_spec(qlib_kernels=1)) == baseline
    assert _identity_of(_identity_spec(scan_batch_rows=1024)) == baseline
