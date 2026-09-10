from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
import numpy as np

REPO_BACKEND = Path(__file__).resolve().parents[1]

from app.experiments.feature_shard_cache import MatrixSegment
from app.experiments.qlib_lightgbm_direct import (
    DateRange,
    EXPANDING,
    DirectExperimentConfigError,
    DirectExperimentDataError,
    DirectPredictionConfig,
    FoldOutcome,
    _RollingFold,
    _build_rolling_plan,
    _decorate_daily_metrics,

    assemble_sharded_dataset,
    run_direct_prediction,
    run_fold,
)
from app.research.model_definition import resolve_model_params
from app.research.model_evaluation import evaluate_segment, score_frame
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
from scripts.qlib_lightgbm_direct import _predict_config, build_parser


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


def test_open_provider_continues_past_a_batch_without_research_prices(
    tmp_path, monkeypatch
) -> None:
    path = _write_provider(tmp_path)
    lines = [
        f"untradable-{index:02d}\t2025-01-06\t2025-01-07\n" for index in range(64)
    ]
    lines.append(f"{SNAPSHOT_ID}\t2025-01-06\t2025-01-07\n")
    (path / "instruments" / "all.txt").write_text("".join(lines), encoding="utf-8")
    calls = []

    def fake_read_features(bundle_path, **kwargs):
        calls.append(kwargs["instruments"])
        close = [float("nan")] if len(calls) == 1 else [100.0]
        return pd.DataFrame({"$close": close, "$factor": [1.0], "$vwap": [100.0]})

    monkeypatch.setattr("app.research.day_provider.read_features", fake_read_features)

    opened = open_day_provider(SNAPSHOT_ID, tmp_path)

    assert opened.path == path
    assert len(calls) == 2
    assert calls[-1] == [str(SNAPSHOT_ID)]


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
        _build_rolling_plan(
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


def test_label_maturity_stops_at_snapshot_bar_coverage() -> None:
    calendar = [date(2025, 1, day) for day in range(2, 16)]
    frame = pd.DataFrame(
        {
            "observation_date": [date(2025, 1, 8)],
            "factor_coverage": [1.0],
            "label_coverage": [0.0],
            "valid_factor_count": [100],
            "valid_label_count": [0],
            "ic": [None],
            "unavailable_reasons": [["insufficient_label_coverage"]],
        }
    )
    provider = SimpleNamespace(
        manifest=SimpleNamespace(
            calendar=tuple(calendar),
            coverage_end=calendar[-1],
        )
    )

    result = _decorate_daily_metrics(
        frame,
        provider,
        {date(2025, 1, 8): 100},
        label_horizon=5,
        snapshot_coverage_end=date(2025, 1, 10),
    )

    assert "label_not_mature" in result.iloc[0]["unavailable_reasons"]


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
    # Defaults now live in the resolver, so that `--config` can fill a field the
    # flags left out without argparse having already answered for it.
    resolved = _predict_config(parser, parsed)
    assert resolved.feature_set == "alpha158"
    assert resolved.label_horizon == 5
    assert resolved.rolling_step == 20
    assert resolved.stop_metric == "l2"
    assert resolved.train_window == EXPANDING
    assert resolved.model_params is None


class _FakeBooster:
    """As much of a LightGBM booster as the artifact path uses: it saves a file."""

    best_iteration = 3

    def __init__(self, number: int) -> None:
        self.number = number

    def save_model(self, path, *, num_iteration):
        Path(path).write_text(
            f"fold {self.number} saved at iteration {num_iteration}", encoding="utf-8"
        )


@pytest.fixture
def direct_run(monkeypatch, tmp_path):
    """One whole run with Qlib, the database and LightGBM replaced by fakes.

    What stays real is the sequence `run_direct_prediction` itself performs, which
    is what the tests below are about. Shared rather than repeated: the setup is
    long enough that a second copy would drift from this one.

    `cache_root` is handed back with it. Left to its default the run caches its
    stock pool beside the configured provider -- a real directory outside the test
    -- where a stale pool would satisfy a later run and the fake below would never
    be called.
    """
    days = list(pd.bdate_range("2025-01-01", "2025-06-30").date)
    snapshot = SimpleNamespace(
        id=SNAPSHOT_ID,
        source="jquants",
        is_backtest_eligible=True,
        calendar_publication_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        bar_publish_sequence=7,
        coverage_start=days[0],
        coverage_end=days[-1],
        version=1,
    )

    class ReadOnlySession:
        def __init__(self):
            self.get_calls = []

        def get(self, model, identity):
            self.get_calls.append((model, identity))
            return snapshot

    session = ReadOnlySession()
    calendar = _Calendar(days)
    config = _config()
    instruments = [f"instrument-{index:03d}" for index in range(100)]
    provider = SimpleNamespace(
        path="provider",
        manifest=SimpleNamespace(
            calendar_publication_id=str(snapshot.calendar_publication_id),
            snapshot_bar_publish_sequence=snapshot.bar_publish_sequence,
            logical_checksum="a" * 64,
            calendar=tuple(days),
            coverage_end=days[-1],
        ),
    )
    feature_set = SimpleNamespace(
        max_window=1,
        features=("F0",),
        column_names=("F0",),
        definition_checksum="feature-checksum",
    )

    monkeypatch.setattr(
        "app.experiments.qlib_lightgbm_direct.SessionCalendarPort",
        lambda *args: calendar,
    )
    monkeypatch.setattr(
        "app.experiments.qlib_lightgbm_direct.open_day_provider",
        lambda *args: provider,
    )
    monkeypatch.setattr(
        "app.experiments.qlib_lightgbm_direct.resolve_feature_set",
        lambda *args: feature_set,
    )

    def fake_universe(session_arg, snapshot_arg, calendar_arg, days, max_window):
        all_dates = sorted(days)
        return (
            {day: set(instruments) for day in all_dates},
            {day: len(instruments) for day in all_dates},
            {instrument: instrument for instrument in instruments},
            [],
            "pool-fingerprint",
        )

    labels = pd.DataFrame(
        [
            {
                "observation_date": day,
                "instrument_id": instrument,
                "label": float(index),
                "label_reason": None,
            }
            for day in calendar.open_days_between(config.test.start, config.test.end)
            for index, instrument in enumerate(instruments)
        ]
    )
    monkeypatch.setattr(
        "app.experiments.qlib_lightgbm_direct._build_universe", fake_universe
    )

    def segment_for(bounds, learning):
        days = calendar.open_days_between(bounds.start, bounds.end)
        index = pd.MultiIndex.from_product(
            [pd.to_datetime(days), instruments], names=["datetime", "instrument"]
        )
        values = np.tile(np.arange(len(instruments), dtype="float32"), len(days))
        return MatrixSegment(
            values.reshape(-1, 1), values.astype("float64"), index, ("F0",)
        )

    dataset = SimpleNamespace(
        root=tmp_path / "cache",
        load_segment=lambda bounds, *, learning: segment_for(bounds, learning),
        raw_labels=lambda: labels,
    )
    required = []

    def fake_assemble(*args, required_days, **kwargs):
        required.append(required_days)
        return dataset

    monkeypatch.setattr(
        "app.experiments.qlib_lightgbm_direct.assemble_sharded_dataset", fake_assemble
    )
    trained_folds = []

    def fake_run_fold(number, train, valid, test, raw_labels, sizes, **kwargs):
        trained_folds.append(number)
        scores = pd.Series(
            test.features[:, 0].astype("float64"), index=test.index, name="score"
        )
        daily = evaluate_segment(
            score_frame(scores), raw_labels, universe_sizes=sizes,
            segment="test", source="lightgbm",
        )
        daily.insert(0, "fold", number)
        return FoldOutcome(
            number, _FakeBooster(number), scores, daily,
            pd.DataFrame({"fold": [number], "feature_name": ["F0"], "gain": [1.0], "split": [1]}),
            pd.DataFrame({"fold": [number], "dataset": ["valid"], "metric": ["l2"],
                          "iteration": [1], "value": [0.5]}),
            {"fold": number, "best_iteration": 3, "train_rows": len(train),
             "valid_rows": len(valid), "test_rows": len(test)},
        )

    monkeypatch.setattr("app.experiments.qlib_lightgbm_direct.run_fold", fake_run_fold)

    return SimpleNamespace(
        session=session,
        config=config,
        calendar=calendar,
        cache_root=tmp_path / "cache-root",
        trained_folds=trained_folds,
        required=required,
    )


def test_direct_prediction_retrains_each_fold_and_only_reads_session(direct_run) -> None:
    session, config, calendar = direct_run.session, direct_run.config, direct_run.calendar
    trained_folds, required = direct_run.trained_folds, direct_run.required

    result = run_direct_prediction(session, config, cache_root=direct_run.cache_root)

    assert trained_folds == [1, 2]
    # The run states which days it needs as a condition of getting a reader.
    assert required and required[0] >= set(
        calendar.open_days_between(config.test.start, config.test.end)
    )
    assert result.summary["fold_count"] == 2
    assert result.summary["test_ic_mean"] == pytest.approx(1.0)
    assert result.predictions["datetime"].nunique() == 40
    assert len(session.get_calls) == 1
    assert session.get_calls[0][1] == SNAPSHOT_ID
    assert not hasattr(session, "add")
    assert not hasattr(session, "commit")


def test_an_artifact_directory_receives_the_learning_curves(direct_run, tmp_path) -> None:
    """The only caller that asks for artifacts is a search, and it ranks on these.

    Worth its own test because the search suite fakes this whole function out and
    writes the file itself, so no test reached the real write: a call here to a
    function that did not exist survived review and every test run, and failed on
    the first real search instead.
    """
    artifacts = tmp_path / "artifacts"

    run_direct_prediction(
        direct_run.session, direct_run.config,
        cache_root=direct_run.cache_root, artifact_dir=artifacts,
    )

    written = pd.read_parquet(artifacts / "learning_curves.parquet")
    assert list(written.columns) == ["fold", "dataset", "metric", "iteration", "value"]
    assert sorted(written["fold"].unique()) == [1, 2]
    assert sorted(path.name for path in (artifacts / "models").iterdir()) == [
        "fold-1.txt", "fold-2.txt",
    ]


def test_without_an_artifact_directory_the_run_writes_no_curves(direct_run, tmp_path) -> None:
    """The command line prints a summary and asks for no files; it must leave none."""
    before = sorted(path.name for path in tmp_path.iterdir())

    run_direct_prediction(direct_run.session, direct_run.config, cache_root=direct_run.cache_root)

    assert not list(tmp_path.rglob("learning_curves.parquet"))
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted({*before, "cache-root"})


# ---------------------------------------------------------------------------
# The search and the direct run must be the same model, or a searched candidate
# is only an answer about the search.
# ---------------------------------------------------------------------------


def _grouped_frame(days, instruments, seed):
    rng = np.random.default_rng(seed)
    index = pd.MultiIndex.from_product(
        [pd.to_datetime(days), instruments], names=["datetime", "instrument"]
    )
    features = rng.normal(size=(len(index), 4))
    labels = features[:, 0] * 0.7 - features[:, 2] * 0.3 + rng.normal(scale=0.4, size=len(index))
    columns = pd.MultiIndex.from_tuples(
        [("feature", f"F{item}") for item in range(4)] + [("label", "LABEL0")]
    )
    return pd.DataFrame(np.column_stack([features, labels]), index=index, columns=columns)


MODEL_PARAMS = {
    "learning_rate": 0.03,
    "num_leaves": 7,
    "max_depth": 3,
    "min_data_in_leaf": 120,
    "feature_fraction": 0.7,
    "bagging_fraction": 0.9,
    "bagging_freq": 1,
    "lambda_l1": 5.0,
    "lambda_l2": 1.5,
    "num_boost_round": 60,
    "early_stopping_rounds": 10,
}


def test_a_rejected_model_parameter_fails_before_any_training(monkeypatch) -> None:
    """The override whitelist is the direct run's, not something the search can widen."""
    days = list(pd.bdate_range("2025-01-01", "2025-06-30").date)
    snapshot = SimpleNamespace(
        id=SNAPSHOT_ID, source="jquants", is_backtest_eligible=True,
        calendar_publication_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        bar_publish_sequence=7, coverage_start=days[0], coverage_end=days[-1], version=1,
    )
    session = SimpleNamespace(get=lambda model, identity: snapshot)
    with pytest.raises(DirectExperimentConfigError, match="num_leaves"):
        run_direct_prediction(session, _config(model_params={"num_leaves": 9999}))
    with pytest.raises(DirectExperimentConfigError, match="seed"):
        run_direct_prediction(session, _config(model_params={"seed": 5}))


def test_a_fixed_train_window_slides_instead_of_expanding() -> None:
    days = []
    current = date(2025, 1, 1)
    while current <= date(2025, 12, 31):
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    calendar = _Calendar(days)
    snapshot = SimpleNamespace(coverage_start=days[0], coverage_end=days[-1])
    shared = dict(
        train=DateRange(days[0], days[59]),
        valid=DateRange(days[70], days[89]),
        test=DateRange(days[100], days[139]),
        label_horizon=5,
        rolling_step=20,
    )
    expanding, _ = _build_rolling_plan(snapshot, calendar, _config(**shared))
    windowed, _ = _build_rolling_plan(
        snapshot, calendar, _config(**shared, train_window=60)
    )

    assert [fold.train.start for fold in expanding] == [days[0]] * len(expanding)
    assert expanding[0].train.start == windowed[0].train.start
    # The window slides with the fold, so every fold trains on the same length.
    lengths = {
        len(calendar.open_days_between(fold.train.start, fold.train.end)) for fold in windowed
    }
    assert lengths == {60}
    assert windowed[-1].train.start > windowed[0].train.start


def test_a_train_window_longer_than_the_first_fold_is_refused() -> None:
    days = []
    current = date(2025, 1, 1)
    while current <= date(2025, 12, 31):
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    calendar = _Calendar(days)
    snapshot = SimpleNamespace(coverage_start=days[0], coverage_end=days[-1])
    with pytest.raises(DirectExperimentConfigError, match="train_window"):
        _build_rolling_plan(
            snapshot,
            calendar,
            _config(
                train=DateRange(days[0], days[59]),
                valid=DateRange(days[70], days[89]),
                test=DateRange(days[100], days[139]),
                label_horizon=5,
                rolling_step=20,
                train_window=200,
            ),
        )


def test_a_searched_candidate_file_round_trips_into_a_run_config(tmp_path) -> None:
    """Item 3 of the loop: what the search exports is what the CLI executes."""
    from app.experiments.search_report import direct_run_config
    from app.experiments.artifact_cache import write_json

    root = tmp_path / "experiment"
    write_json(root / "manifest.json", {"config": {
        "snapshot_id": str(SNAPSHOT_ID), "provider_root": "/app/var/qlib-cli-data",
        "purge_horizon": 20, "step": 20, "min_train_days": 252, "num_threads": 2,
    }})
    write_json(root / "fold_plan.json", {"folds": [
        {"fold": 1, "train": ["2024-09-02", "2025-09-11"], "train_days": 252,
         "valid": ["2025-10-16", "2026-01-15"], "evaluation": ["2026-02-17", "2026-03-17"]},
        {"fold": 2, "train": ["2024-09-02", "2025-10-14"], "train_days": 272,
         "valid": ["2025-11-14", "2026-02-13"], "evaluation": ["2026-03-18", "2026-04-21"]},
    ]})
    trial = {"trial_id": "0b61155b01509d06732e", "feature_set": "alpha360", "horizon": 20,
             "train_window": 252, "stop_metric": "l2", "seed": 20260829,
             "model_params": {"num_leaves": 7, "max_depth": 3}}

    exported = direct_run_config(root, trial)
    assert exported["test"] == "2026-02-17:2026-04-21"
    assert exported["train"] == "2024-09-02:2025-09-11"

    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(exported), encoding="utf-8")
    parser = build_parser()
    resolved = _predict_config(parser, parser.parse_args(["predict", "--config", str(path)]))

    assert resolved.feature_set == "alpha360"
    assert resolved.train_window == 252
    assert resolved.stop_metric == "l2"
    assert resolved.label_horizon == 20
    assert resolved.model_params == {"num_leaves": 7, "max_depth": 3}
    assert resolved.test == DateRange(date(2026, 2, 17), date(2026, 4, 21))

    # An explicit flag still wins, so one field can be varied without editing the export.
    overridden = _predict_config(
        parser, parser.parse_args(["predict", "--config", str(path), "--seed", "20260830"])
    )
    assert overridden.seed == 20260830 and overridden.feature_set == "alpha360"


def test_a_shorter_horizon_still_exports_because_the_purge_travels_with_it(tmp_path) -> None:
    """A search purges every horizon by the longest so their dates match.

    Carrying `purge_horizon` into the exported run is what lets a 5-day candidate
    be re-run on the folds it was actually scored on.
    """
    from app.experiments.artifact_cache import write_json
    from app.experiments.search_report import direct_run_config

    root = tmp_path / "experiment"
    write_json(root / "manifest.json", {"config": {
        "snapshot_id": str(SNAPSHOT_ID), "provider_root": "unused", "purge_horizon": 20,
        "step": 20, "min_train_days": 252, "num_threads": 2,
    }})
    write_json(root / "fold_plan.json", {"folds": [
        {"fold": 1, "train": ["2024-09-02", "2025-09-11"], "train_days": 252,
         "valid": ["2025-10-16", "2026-01-15"], "evaluation": ["2026-02-17", "2026-03-17"]},
    ]})
    base = {"trial_id": "t", "feature_set": "alpha158", "train_window": "expanding",
            "stop_metric": "l2", "seed": 1, "model_params": {}}

    exported = direct_run_config(root, {**base, "horizon": 5})
    assert exported["label_horizon"] == 5
    assert exported["purge_horizon"] == 20

    with pytest.raises(ValueError, match="train window"):
        direct_run_config(root, {**base, "horizon": 20, "train_window": 400})


@pytest.mark.parametrize("prefix", ["", "﻿"])
def test_json_file_arguments_survive_a_powershell_written_bom(tmp_path, prefix) -> None:
    """PowerShell 5.1 writes a BOM for its own `utf8`, and a file is exactly the
    route a Windows caller takes when the shell mangles inline JSON."""
    from scripts.qlib_lightgbm_direct import _json_object

    params = tmp_path / "model-params.json"
    params.write_bytes((prefix + json.dumps({"num_leaves": 7})).encode("utf-8"))
    assert _json_object(f"@{params}") == {"num_leaves": 7}

    config = tmp_path / "run.json"
    config.write_bytes(
        (prefix + json.dumps({
            "snapshot_id": str(SNAPSHOT_ID), "train": "2026-01-05:2026-01-09",
            "valid": "2026-01-13:2026-01-16", "test": "2026-01-20:2026-01-23",
            "feature_set": "alpha360",
        })).encode("utf-8")
    )
    parser = build_parser()
    resolved = _predict_config(parser, parser.parse_args(["predict", "--config", str(config)]))
    assert resolved.feature_set == "alpha360"


def test_parsing_arguments_does_not_import_qlib() -> None:
    """`predict > file.json` has to produce parseable JSON.

    Importing Qlib announces its absent optional backends on stdout. `main` does
    that import with stdout pointed at stderr, but argparse type functions run
    during `parse_args`, before that redirect -- so one of them importing the
    experiment module put three lines of chatter at the top of every output file.
    """
    import subprocess

    source = (
        "import sys;"
        "sys.path.insert(0, '.');"
        "from scripts.qlib_lightgbm_direct import build_parser;"
        "build_parser().parse_args(["
        "  'predict', '--snapshot-id', '11111111-1111-1111-1111-111111111111',"
        "  '--train', '2026-01-05:2026-01-09', '--valid', '2026-01-13:2026-01-16',"
        "  '--test', '2026-01-20:2026-01-23', '--train-window', '252']);"
        "print('qlib' in sys.modules, 'app.experiments.qlib_lightgbm_direct' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, cwd=REPO_BACKEND
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False False"


def test_structlog_output_does_not_reach_stdout(monkeypatch, capsys) -> None:
    """The application logs through structlog, whose stock console renderer prints
    to stdout. A run emits hundreds of `stock_pool.built` lines, so without this
    the redirected result file is logs and no JSON at all."""
    from app.core.logging import get_logger
    from scripts import qlib_lightgbm_direct as cli

    class _NullSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def logged_run(session, config):
        get_logger(__name__).info("stock_pool.built", as_of="2026-04-21", members=739)
        return SimpleNamespace(
            summary={"ok": True}, daily_ic=pd.DataFrame(),
            predictions=pd.DataFrame(), feature_importance=pd.DataFrame(),
        )

    monkeypatch.setattr(cli, "get_sessionmaker", lambda: lambda: _NullSession())
    monkeypatch.setattr(
        "app.experiments.qlib_lightgbm_direct.run_direct_prediction", logged_run
    )
    cli.main([
        "predict", "--snapshot-id", str(SNAPSHOT_ID),
        "--train", "2026-01-05:2026-01-09", "--valid", "2026-01-13:2026-01-16",
        "--test", "2026-01-20:2026-01-23",
    ])
    captured = capsys.readouterr()
    assert "stock_pool.built" in captured.err
    assert json.loads(captured.out) == {
        "summary": {"ok": True}, "daily_ic": [], "predictions": [], "feature_importance": [],
    }


def test_predict_writes_one_json_document_to_stdout(monkeypatch, capsys) -> None:
    from scripts import qlib_lightgbm_direct as cli

    class _NullSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(cli, "get_sessionmaker", lambda: lambda: _NullSession())
    monkeypatch.setattr(
        "app.experiments.qlib_lightgbm_direct.run_direct_prediction",
        lambda session, config: SimpleNamespace(
            summary={"feature_set": config.feature_set, "window": config.train_window},
            daily_ic=pd.DataFrame(),
            predictions=pd.DataFrame(),
            feature_importance=pd.DataFrame(),
        ),
    )
    assert cli.main([
        "predict", "--snapshot-id", str(SNAPSHOT_ID),
        "--train", "2026-01-05:2026-01-09", "--valid", "2026-01-13:2026-01-16",
        "--test", "2026-01-20:2026-01-23", "--feature-set", "alpha360",
        "--train-window", "252",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"] == {"feature_set": "alpha360", "window": 252}


def test_the_cli_and_the_module_agree_on_the_expanding_literal() -> None:
    """The CLI restates the constant so argparse need not import the module."""
    from scripts.qlib_lightgbm_direct import EXPANDING as cli_expanding

    assert cli_expanding == EXPANDING




def test_the_exported_config_keeps_container_paths_posix(tmp_path) -> None:
    """The export is written on one machine and read inside a container.

    `str(Path("/app/var/x"))` on Windows yields backslashes, which the container
    would take as a relative name and fail to find.
    """
    from app.experiments.artifact_cache import write_json
    from app.experiments.search_report import direct_run_config

    root = tmp_path / "experiment"
    write_json(root / "manifest.json", {"config": {
        "snapshot_id": str(SNAPSHOT_ID), "provider_root": "/app/var/qlib-cli-data",
        "purge_horizon": 20, "step": 20, "min_train_days": 252, "num_threads": 2,
    }})
    write_json(root / "fold_plan.json", {"folds": [
        {"fold": 1, "train": ["2024-09-02", "2025-09-11"], "train_days": 252,
         "valid": ["2025-10-16", "2026-01-15"], "evaluation": ["2026-02-17", "2026-03-17"]},
    ]})
    exported = direct_run_config(root, {
        "trial_id": "t", "feature_set": "alpha360", "horizon": 20, "train_window": 252,
        "stop_metric": "l2", "seed": 1, "model_params": {},
    })

    assert exported["provider_root"] == "/app/var/qlib-cli-data"
    assert "\\" not in exported["provider_root"]


def test_the_exported_config_refuses_a_field_it_has_no_rule_for() -> None:
    """A configuration field the renderer has no rule for must stop the export.

    `write_json` would otherwise stringify it and the command line would read the
    string back as itself.
    """
    import dataclasses
    from app.experiments.qlib_lightgbm_direct import DateRange
    from app.experiments.search_report import _render_run_config

    @dataclasses.dataclass(frozen=True)
    class WithUnknownField:
        train: DateRange
        oddity: object

    with pytest.raises(ValueError, match="No rendering rule for 'oddity'"):
        _render_run_config(
            WithUnknownField(DateRange(date(2026, 1, 5), date(2026, 1, 9)), {1, 2})
        )
