from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
import numpy as np

from app.experiments.qlib_lightgbm_direct import (
    DateRange,
    DirectExperimentConfigError,
    DirectPredictionConfig,
    _RollingFold,
    _build_dataset,
    _build_rolling_plan,
    _dataset_for_fold,
    _decorate_daily_metrics,
    _train,
    _validate_dates,
)
from app.research.day_provider import (
    DayProviderError,
    delete_day_provider,
    open_day_provider,
    provider_path,
)
from app.research.day_provider_export import (
    EXPORTER_SCHEMA_VERSION,
    PYQLIB_VERSION,
    DayProviderManifest,
    load_day_provider_manifest,
)
from app.services.calendar_port import CalendarCoverageError
from scripts.qlib_lightgbm_direct import build_parser


SNAPSHOT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _manifest(snapshot_id: uuid.UUID = SNAPSHOT_ID) -> DayProviderManifest:
    days = (date(2025, 1, 6), date(2025, 1, 7))
    return DayProviderManifest(
        snapshot_id=str(snapshot_id),
        snapshot_bar_publish_sequence=7,
        calendar_publication_id="22222222-2222-2222-2222-222222222222",
        exporter_schema_version=EXPORTER_SCHEMA_VERSION,
        pyqlib_version=PYQLIB_VERSION,
        coverage_start=days[0],
        coverage_end=days[-1],
        instrument_count=1,
        fields=("close", "factor", "vwap"),
        logical_checksum="a" * 64,
        rows=2,
        instruments=(str(SNAPSHOT_ID),),
        calendar=days,
    )


def _write_provider(root, manifest: DayProviderManifest | None = None):
    manifest = manifest or _manifest()
    path = provider_path(SNAPSHOT_ID, root)
    (path / "calendars").mkdir(parents=True)
    (path / "instruments").mkdir()
    (path / "features").mkdir()
    (path / "calendars" / "day.txt").write_text("2025-01-06\n2025-01-07\n", encoding="utf-8")
    (path / "instruments" / "all.txt").write_text(
        f"{SNAPSHOT_ID}\t2025-01-06\t2025-01-07\n", encoding="utf-8"
    )
    (path / "manifest.json").write_text(
        json.dumps(manifest.to_dict()), encoding="utf-8"
    )
    return path


def test_manifest_round_trip_preserves_provider_identity(tmp_path) -> None:
    manifest = _manifest()
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")

    assert load_day_provider_manifest(path) == manifest


def test_open_provider_uses_manifest_structure_and_smoke_read(tmp_path, monkeypatch) -> None:
    path = _write_provider(tmp_path)
    called = {}

    def fake_read_features(bundle_path, **kwargs):
        called.update({"path": bundle_path, **kwargs})
        return pd.DataFrame({"$close": [100.0], "$factor": [1.0], "$vwap": [100.0]})

    monkeypatch.setattr("app.research.day_provider.read_features", fake_read_features)

    opened = open_day_provider(SNAPSHOT_ID, tmp_path)

    assert opened.path == path
    assert called["instruments"] == [str(SNAPSHOT_ID)]
    assert called["fields"] == ["$close", "$factor", "$vwap"]


def test_delete_validates_every_version_manifest_before_removing(tmp_path) -> None:
    current = _write_provider(tmp_path)
    old = current.parent / "schema-1_pyqlib-0.9.7"
    old.mkdir()
    (old / "manifest.json").write_text(json.dumps(_manifest().to_dict()), encoding="utf-8")

    assert delete_day_provider(SNAPSHOT_ID, tmp_path) is True
    assert not current.parent.exists()
    assert delete_day_provider(SNAPSHOT_ID, tmp_path) is False


def test_delete_refuses_when_any_version_manifest_is_corrupt(tmp_path) -> None:
    current = _write_provider(tmp_path)
    corrupt = current.parent / "schema-old"
    corrupt.mkdir()
    (corrupt / "manifest.json").write_text("not json", encoding="utf-8")

    with pytest.raises(DayProviderError, match="Cannot read"):
        delete_day_provider(SNAPSHOT_ID, tmp_path)

    assert current.parent.exists()


class _Calendar:
    def __init__(self, days):
        self.days = list(days)

    def is_open(self, day):
        return day in self.days

    def next_open(self, day):
        return self.days[self.days.index(day) + 1]

    def open_days_between(self, start, end):
        if start < self.days[0] or end > self.days[-1]:
            raise CalendarCoverageError(
                f"{start}..{end} is outside {self.days[0]}..{self.days[-1]}"
            )
        return [day for day in self.days if start <= day <= end]


def _config(**changes):
    values = {
        "snapshot_id": SNAPSHOT_ID,
        "feature_set": "alpha158",
        "train": DateRange(date(2025, 1, 2), date(2025, 1, 31)),
        "valid": DateRange(date(2025, 2, 3), date(2025, 2, 28)),
        "test": DateRange(date(2025, 3, 3), date(2025, 4, 25)),
    }
    values.update(changes)
    return DirectPredictionConfig(**values)


def test_rolling_plan_expands_train_slides_valid_and_purges_labels() -> None:
    days = []
    current = date(2025, 1, 1)
    while current <= date(2025, 6, 30):
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    snapshot = SimpleNamespace(coverage_start=days[0], coverage_end=days[-1])

    folds, resolved = _build_rolling_plan(snapshot, _Calendar(days), _config())

    assert len(folds) == 2
    assert folds[0] == _RollingFold(
        1,
        DateRange(date(2025, 1, 2), date(2025, 1, 23)),
        DateRange(date(2025, 2, 3), date(2025, 2, 20)),
        DateRange(date(2025, 3, 3), date(2025, 3, 28)),
    )
    assert folds[1] == _RollingFold(
        2,
        DateRange(date(2025, 1, 2), date(2025, 2, 20)),
        DateRange(date(2025, 3, 3), date(2025, 3, 20)),
        DateRange(date(2025, 3, 31), date(2025, 4, 25)),
    )
    assert len(resolved["fold_test"][1]) == 20
    assert len(resolved["fold_test"][2]) == 20


def test_rolling_plan_does_not_query_calendar_from_earlier_snapshot_start() -> None:
    days = []
    current = date(2025, 1, 1)
    while current <= date(2025, 6, 30):
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    snapshot = SimpleNamespace(
        coverage_start=date(2024, 12, 31),
        coverage_end=days[-1],
    )

    folds, _ = _build_rolling_plan(snapshot, _Calendar(days), _config())

    assert len(folds) == 2
    assert folds[0].train.start == date(2025, 1, 2)


def test_rolling_plan_rejects_invalid_endpoint_order() -> None:
    days = [date(2025, 1, 1) + timedelta(days=offset) for offset in range(180)]
    days = [day for day in days if day.weekday() < 5]
    snapshot = SimpleNamespace(coverage_start=days[0], coverage_end=days[-1])

    with pytest.raises(DirectExperimentConfigError, match="ordered"):
        _validate_dates(
            snapshot,
            _Calendar(days),
            _config(valid=DateRange(date(2025, 1, 20), date(2025, 2, 28))),
        )


def test_daily_metrics_keep_tail_diagnostics_but_mark_it_ineligible() -> None:
    frame = pd.DataFrame(
        {
            "observation_date": [date(2025, 1, 6), date(2025, 1, 7)],
            "factor_coverage": [1.0, 1.0],
            "label_coverage": [1.0, 0.0],
            "valid_factor_count": [100, 100],
            "valid_label_count": [100, 0],
            "ic": [0.1, None],
            "unavailable_reasons": [[], ["insufficient_label_coverage"]],
        }
    )
    provider = SimpleNamespace(manifest=_manifest())

    result = _decorate_daily_metrics(
        frame, provider, {date(2025, 1, 6): 100, date(2025, 1, 7): 100}
    )

    assert result["ic_eligible"].tolist() == [True, False]
    assert "label_not_mature" in result.iloc[1]["unavailable_reasons"]
    assert result["pool_size"].tolist() == [100, 100]


def test_cli_exposes_three_independent_subcommands() -> None:
    parser = build_parser()

    for command in ("build-provider", "delete-provider"):
        parsed = parser.parse_args([command, "--snapshot-id", str(SNAPSHOT_ID)])
        assert parsed.command == command
    parsed = parser.parse_args(
        [
            "predict",
            "--snapshot-id",
            str(SNAPSHOT_ID),
            "--train",
            "2025-01-02:2025-01-03",
            "--valid",
            "2025-01-08:2025-01-10",
            "--test",
            "2025-01-15:2025-01-17",
        ]
    )
    assert parsed.command == "predict"
    assert parsed.feature_set == "alpha158"
    assert parsed.label_horizon == 5
    assert parsed.rolling_step == 20


def test_daily_dataset_keeps_prediction_only_tail_in_inference(monkeypatch) -> None:
    days = [date(2025, 1, day) for day in range(2, 18) if date(2025, 1, day).weekday() < 5]
    instruments = [str(SNAPSHOT_ID), "22222222-2222-2222-2222-222222222222"]
    index = pd.MultiIndex.from_product(
        [instruments, pd.to_datetime(days)], names=["instrument", "datetime"]
    )
    raw = pd.DataFrame(
        {
            "Ref($close, 1)": np.arange(len(index), dtype="float64"),
            "$close": np.tile(np.arange(100.0, 100.0 + len(days)), len(instruments)),
        },
        index=index,
    )
    monkeypatch.setattr(
        "app.experiments.qlib_lightgbm_direct.read_features", lambda *args, **kwargs: raw
    )
    feature_set = SimpleNamespace(
        expressions=("Ref($close, 1)",), column_names=("CLOSE_LAG1",)
    )
    config = _config(
        train=DateRange(days[0], days[2]),
        valid=DateRange(days[4], days[6]),
        test=DateRange(days[8], days[-1]),
    )
    universe = {day: set(instruments) for day in days}
    provider = SimpleNamespace(
        path="unused",
        manifest=SimpleNamespace(
            coverage_start=days[0], coverage_end=days[-1], calendar=tuple(days)
        ),
    )

    assembled = _build_dataset(provider, feature_set, config, universe)

    from qlib.data.dataset.handler import DataHandlerLP

    dataset = _dataset_for_fold(
        assembled.handler,
        _RollingFold(1, config.train, config.valid, config.test),
    )
    inferred = dataset.prepare(
        "test", col_set="feature", data_key=DataHandlerLP.DK_I
    )
    assert pd.Timestamp(days[-1]) in inferred.index.get_level_values("datetime")
    tail = assembled.raw_labels[
        assembled.raw_labels["observation_date"] == days[-1]
    ]
    assert set(tail["label_reason"]) == {"label_not_mature"}
    first = assembled.raw_labels[
        assembled.raw_labels["observation_date"] == days[0]
    ]
    expected = (106.0 / 101.0) - 1
    assert np.allclose(first["label"], expected)


def test_native_lightgbm_training_consumes_qlib_grouped_frames() -> None:
    rng = np.random.default_rng(7)

    def grouped(rows):
        feature = rng.normal(size=(rows, 2))
        label = feature[:, 0] * 0.8 - feature[:, 1] * 0.2
        index = pd.MultiIndex.from_arrays(
            [
                pd.to_datetime([date(2025, 1, 2)] * rows),
                [f"instrument-{item}" for item in range(rows)],
            ],
            names=["datetime", "instrument"],
        )
        columns = pd.MultiIndex.from_tuples(
            [("feature", "F0"), ("feature", "F1"), ("label", "LABEL0")]
        )
        return pd.DataFrame(np.column_stack([feature, label]), index=index, columns=columns)

    train = grouped(600)
    valid = grouped(300)
    test = grouped(200)["feature"]

    class FakeDataset:
        def prepare(self, segment, **kwargs):
            return {"train": train, "valid": valid, "test": test}[segment]

    booster, train_rows, valid_rows, test_features = _train(FakeDataset(), 7, 1)

    assert booster.best_iteration > 0
    assert (train_rows, valid_rows, len(test_features)) == (600, 300, 200)
