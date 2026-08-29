"""One model run end to end, on a continuous slice of the real feed.

The unit tests next door each prove one seam. None of them prove the seams add
up: the execution spec, the bundle export, the feature read, the label join, the
training loop and the publish protocol were written against each other's
assumptions, and assumptions are exactly what a unit test cannot check.

So this runs the whole workflow on 150 real securities across 262 continuous
sessions (`.master-backfill/export_model_research_golden.py`), through the
production coverage thresholds — `min_valid_securities = 100` — because a run
that only passes with the thresholds relaxed has not shown it can pass.

No network: ticket 03 keeps ordinary CI off J-Quants.

What is asserted here is *mechanism*, not performance. 16 test cross-sections
cannot show a model is good and nothing below pretends to: the assertions are
that the splits hold, the artifact is committed, the rows agree with the bytes,
and the momentum control was scored on the very same rows.
"""

from __future__ import annotations

import gzip
import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.models.market_data import (
    BarObservationDisposition,
    BarQualityStatus,
    BarRecord,
    BarVersion,
    DataSnapshot,
    EndpointPublication,
    Instrument,
    InstrumentMasterSnapshot,
    InstrumentMasterSnapshotMember,
    PublicationBarObservation,
    PublicationStatus,
    SyncRun,
    SyncRunStatus,
    TradingCalendar,
)
from app.models.research import (
    ArtifactPublicationStatus,
    PredictionRun,
    ResearchArtifactPublication,
    ResearchExperiment,
    ResearchExperimentKind,
    ResearchRun,
    ResearchRunStatus,
    TrainedModel,
)
from app.models.task import Task, TaskStatus
from app.research.feature_sets import get_feature_set
from app.research.model_definition import (
    DEFAULT_SEED,
    ModelResearchDefinition,
    resolve_model_params,
)
from app.research.model_workflow import MODEL_SOURCE, MOMENTUM_SOURCE, ModelResearchWorkflow
from app.research.splits import build_split_plan, propose_split
from app.services.calendar_normalization import FULL_DAY
from app.services.stock_pool import DEFAULT_POLICY, policy_fingerprint

pytest.importorskip("qlib")

GOLDEN = Path(__file__).parent / "data" / "model_research_golden.json.gz"
pytestmark = pytest.mark.skipif(not GOLDEN.exists(), reason="golden fixture not exported")

#: What the exporter was asked for. Pinned so a silently re-exported fixture is
#: caught here rather than showing up as a changed metric later.
EXPECTED_SESSIONS = 262
EXPECTED_SECURITIES = 150

#: The feature set the ticket names as the baseline. Deliberately not a reduced
#: stand-in: 158 columns over 150 securities is the shape the chain has to
#: survive, and a four-column version would not exercise the kbar group, the
#: `$vwap` column, or the 60-session rolling windows.
FEATURE_SET = "alpha158_jp_v1"


@pytest.fixture(scope="module")
def golden() -> dict:
    with gzip.open(GOLDEN, "rt", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def loaded(golden, engine):
    """Rebuild the slice as ordinary rows, including a real trading calendar.

    Ticket 05's golden test hands its pool an in-memory calendar port. This one
    cannot: the workflow reads `TradingCalendar` through a session, so the
    calendar has to exist as rows or the run under test is not the run that
    happens in production.
    """
    connection = engine.connect()
    outer = connection.begin()
    session = Session(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    now = datetime.now(timezone.utc)
    task = Task(task_type="jquants_sync", payload={}, progress={})
    session.add(task)
    session.flush()
    run = SyncRun(
        task_id=task.id,
        source="jquants",
        status=SyncRunStatus.SUCCEEDED,
        idempotency_key=str(uuid.uuid4()),
    )
    session.add(run)
    session.flush()

    def publication(endpoint: str, sequence: int) -> EndpointPublication:
        row = EndpointPublication(
            sync_run_id=run.id,
            created_by_task_id=task.id,
            created_by_task_attempt=1,
            endpoint=endpoint,
            scope_ordinal=sequence,
            status=PublicationStatus.PUBLISHED,
            publish_sequence=sequence,
            adapter_version="golden",
            published_at=now,
        )
        session.add(row)
        return row

    calendar_publication = publication("markets/calendar", 1)
    bars_publication = publication("equities/bars/daily", 2)
    master_publication = publication("equities/master", 3)
    session.flush()

    session.execute(
        TradingCalendar.__table__.insert(),
        [
            {
                "id": uuid.uuid4(),
                "publication_id": calendar_publication.id,
                "market": "TSE",
                "trade_date": date.fromisoformat(day),
                "is_open": True,
                "session": FULL_DAY,
                "hol_div": "1",
            }
            for day in golden["calendar"]
        ],
    )

    instruments = {row["symbol"]: uuid.uuid4() for row in golden["roster"]}
    session.execute(
        Instrument.__table__.insert(),
        [
            {"instrument_id": value, "source": "jquants", "source_code": symbol}
            for symbol, value in instruments.items()
        ],
    )

    roster = InstrumentMasterSnapshot(
        source="jquants",
        as_of_date=date.fromisoformat(golden["roster_as_of"]),
        sync_run_id=run.id,
        publication_id=master_publication.id,
    )
    session.add(roster)
    session.flush()
    session.execute(
        InstrumentMasterSnapshotMember.__table__.insert(),
        [
            {
                "id": uuid.uuid4(),
                "snapshot_id": roster.id,
                "instrument_id": instruments[row["symbol"]],
                "symbol": row["symbol"],
                "market_code": row["market_code"],
                "product_category": row["product_category"],
                "inferred_security_class": "unused",
                "content_hash": "unused",
            }
            for row in golden["roster"]
        ],
    )

    records, versions, observations = [], [], []
    for row in golden["bars"]:
        record_id, version_id = uuid.uuid4(), uuid.uuid4()
        records.append(
            {
                "id": record_id,
                "source": "jquants",
                "instrument_id": instruments[row["symbol"]],
                "trade_date": date.fromisoformat(row["date"]),
                "session": FULL_DAY,
            }
        )
        versions.append(
            {
                "id": version_id,
                "bar_record_id": record_id,
                "content_hash": str(version_id),
                **{
                    name: _decimal(row[name])
                    for name in (
                        "raw_open", "raw_high", "raw_low", "raw_close", "raw_volume",
                        "adjusted_open", "adjusted_high", "adjusted_low",
                        "adjusted_close", "adjusted_volume",
                        "trading_value", "adjustment_factor",
                    )
                },
                "quality_status": BarQualityStatus(row["quality_status"]),
                "quality_rules": row["quality_rules"],
            }
        )
        observations.append(
            {
                "publication_id": bars_publication.id,
                "bar_record_id": record_id,
                "bar_version_id": version_id,
                "disposition": BarObservationDisposition.NEW,
            }
        )
    session.execute(BarRecord.__table__.insert(), records)
    session.execute(BarVersion.__table__.insert(), versions)
    session.execute(PublicationBarObservation.__table__.insert(), observations)

    snapshot = DataSnapshot(
        source="jquants",
        sync_run_id=run.id,
        bar_publish_sequence=bars_publication.publish_sequence,
        master_publish_sequence=master_publication.publish_sequence,
        calendar_publication_id=calendar_publication.id,
        master_snapshot_id=roster.id,
        coverage_start=date.fromisoformat(golden["calendar"][0]),
        coverage_end=date.fromisoformat(golden["as_of"]),
        plan_fingerprint="model-golden",
    )
    session.add(snapshot)
    session.commit()

    yield session, snapshot
    session.close()
    outer.rollback()
    connection.close()


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


@pytest.fixture(scope="module")
def executed(golden, loaded, tmp_path_factory, monkeypatch_module):
    """Run the workflow once; every assertion below reads its result."""
    session, snapshot = loaded
    sessions = [date.fromisoformat(day) for day in golden["calendar"]]
    # Weekly cross-sections only start once 147 sessions of history exist behind
    # them, which is what the pool policy demands of every member.
    weekly = _weekly(sessions[DEFAULT_POLICY.required_history_days :])
    feature_set = get_feature_set(FEATURE_SET)
    plan = build_split_plan(
        weekly_observations=weekly, calendar=sessions, **propose_split(weekly)
    )
    policy = DEFAULT_POLICY.__class__(
        **{
            **{f.name: getattr(DEFAULT_POLICY, f.name) for f in DEFAULT_POLICY.__dataclass_fields__.values()},
            "required_history_days": 147,
            "required_bar_offsets": (147, 21),
        }
    )
    definition = ModelResearchDefinition(
        data_snapshot_id=snapshot.id,
        feature_set=feature_set,
        split=plan,
        stock_pool_policy_fingerprint=policy_fingerprint(policy),
        model_params=resolve_model_params(None, seed=DEFAULT_SEED),
    )
    experiment = ResearchExperiment(
        definition_fingerprint=definition.fingerprint,
        kind=ResearchExperimentKind.MODEL,
        data_snapshot_id=snapshot.id,
        definition=definition.canonical_payload,
    )
    session.add(experiment)
    session.flush()
    task = Task(task_type="model_research", status=TaskStatus.RUNNING, payload={})
    session.add(task)
    session.flush()
    research_run = ResearchRun(
        experiment_id=experiment.id, task_id=task.id, status=ResearchRunStatus.QUEUED
    )
    session.add(research_run)
    session.commit()

    root = tmp_path_factory.mktemp("model-golden")
    settings = Settings(
        _env_file=None,
        qlib_data_dir=root / "bundles",
        research_artifact_dir=root / "artifacts",
        qlib_threads=1,
    )
    monkeypatch_module.setattr("app.core.config.get_settings", lambda: settings)
    for module in (
        "app.research.model_workflow",
        "app.research.bundle_builder",
        "app.research.publication",
    ):
        monkeypatch_module.setattr(f"{module}.get_settings", lambda: settings, raising=False)

    workflow = ModelResearchWorkflow(lambda: session)
    summary = workflow.execute(research_run.id)
    session.expire_all()
    return session, research_run.id, summary, plan


def _weekly(sessions: list[date]) -> list[date]:
    by_week: dict[tuple[int, int], date] = {}
    for day in sessions:
        by_week[day.isocalendar()[:2]] = day
    return sorted(by_week.values())


# --------------------------------------------------------------------------
# The fixture itself
# --------------------------------------------------------------------------


def test_the_fixture_is_the_one_that_was_reviewed(golden) -> None:
    """A silently re-exported slice would move every metric below."""
    assert golden["export_schema_version"] == "1"
    assert len(golden["calendar"]) == EXPECTED_SESSIONS
    assert len(golden["securities"]) == EXPECTED_SECURITIES
    assert len(golden["bars"]) == EXPECTED_SESSIONS * EXPECTED_SECURITIES
    assert golden["content_checksum"]


def test_the_fixture_is_continuous_not_a_handful_of_dates(golden) -> None:
    """The failing property of ticket 05's fixture, which held 23 discrete days.

    A rolling window, a 147-session history check and a three-way split all
    need an unbroken run of sessions.
    """
    dates = sorted({row["date"] for row in golden["bars"]})

    assert dates == sorted(golden["calendar"])


def test_the_fixture_carries_the_fields_alpha158_reads(golden) -> None:
    sample = golden["bars"][0]

    for field in ("adjusted_open", "adjusted_high", "adjusted_low", "adjusted_close",
                  "adjusted_volume", "trading_value", "raw_close", "raw_volume"):
        assert field in sample


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


def test_the_run_succeeds_through_the_production_thresholds(executed) -> None:
    session, run_id, summary, _ = executed
    run = session.get(ResearchRun, run_id)

    assert run.status is ResearchRunStatus.SUCCEEDED
    assert run.error_code is None
    assert summary["test_observations"] > 0


def test_a_trained_model_and_a_prediction_run_are_recorded(executed) -> None:
    session, run_id, _, plan = executed

    trained = session.scalar(select(TrainedModel).where(TrainedModel.research_run_id == run_id))
    predictions = session.scalar(select(PredictionRun).where(PredictionRun.research_run_id == run_id))

    assert trained is not None
    assert trained.feature_set_name == FEATURE_SET
    assert (trained.fit_start, trained.fit_end) == (plan.train.start, plan.train.end)
    assert trained.model_checksum != trained.model_semantic_checksum
    assert predictions is not None
    assert predictions.trained_model_id == trained.id
    assert predictions.row_count > 0


def test_the_artifact_is_committed_and_its_bytes_are_on_disk(executed) -> None:
    """The publish protocol's whole point, exercised by a real run."""
    session, run_id, _, _ = executed
    from app.core.config import get_settings

    publication = session.scalar(
        select(ResearchArtifactPublication).where(
            ResearchArtifactPublication.research_run_id == run_id
        )
    )
    directory = get_settings().research_artifact_dir / publication.relative_path

    assert publication.status is ArtifactPublicationStatus.COMMITTED
    assert publication.staging_path is None
    assert (directory / "manifest.json").is_file()
    assert (directory / "model.txt").is_file()
    assert (directory / "predictions.parquet").is_file()


def test_the_momentum_control_was_scored_on_the_same_cross_sections(executed) -> None:
    """The comparison is only a comparison if both curves saw the same rows."""
    import pandas as pd
    from app.core.config import get_settings

    session, run_id, _, _ = executed
    publication = session.scalar(
        select(ResearchArtifactPublication).where(
            ResearchArtifactPublication.research_run_id == run_id
        )
    )
    metrics = pd.read_parquet(
        get_settings().research_artifact_dir / publication.relative_path / "metrics.parquet"
    )
    test_rows = metrics[metrics["segment"] == "test"]
    model_dates = set(test_rows[test_rows["source"] == MODEL_SOURCE]["observation_date"])
    momentum_dates = set(test_rows[test_rows["source"] == MOMENTUM_SOURCE]["observation_date"])

    assert model_dates
    assert model_dates == momentum_dates


def test_all_three_segments_are_reported(executed) -> None:
    """Train against test is the overfitting evidence; hiding it hides that."""
    _, _, summary, _ = executed

    segments = {item["segment"] for item in summary["segments"]}

    assert segments == {"train", "valid", "test"}


def test_every_summary_states_how_many_observations_produced_it(executed) -> None:
    _, _, summary, _ = executed

    assert all("observations" in item for item in summary["segments"])
