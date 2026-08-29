"""Crash injection across the publish protocol.

`os.replace` and a database commit cannot happen together, so the protocol is
built to survive a crash at every point between them. These tests kill the
worker at each of those points and assert the state a restart lands in.

The failure the protocol replaces is worth naming: publishing the directory
first and inserting the row afterwards left, on a crash, artifacts on disk that
no row referenced while recovery marked the run failed. "Failed run with
published artifacts" is exactly what the design forbids.
"""

from __future__ import annotations

import uuid
from datetime import date

import pandas as pd
import pytest
from sqlalchemy import select

from app.models.market_data import (
    DataSnapshot,
    EndpointPublication,
    InstrumentMasterSnapshot,
    PublicationStatus,
    SyncRun,
)
from app.models.research import (
    ArtifactPublicationStatus,
    ResearchArtifact,
    ResearchArtifactPublication,
    ResearchExperiment,
    ResearchExperimentKind,
    ResearchRun,
    ResearchRunStatus,
)
from app.models.task import Task, TaskStatus
from app.research.publication import (
    STAGING_PREFIX,
    PublicationError,
    ResearchArtifactPublisher,
    readable_artifact,
    recover_publications,
)


@pytest.fixture()
def snapshot(db_session) -> DataSnapshot:
    """The shortest chain of foreign keys a DataSnapshot will accept.

    None of it matters to the publish protocol; the rows exist only so the
    experiment's snapshot reference resolves.
    """
    task = Task(task_type="jquants_sync", status=TaskStatus.SUCCEEDED, payload={})
    db_session.add(task)
    db_session.flush()
    sync_run = SyncRun(task_id=task.id, source="jquants")
    db_session.add(sync_run)
    db_session.flush()
    publication = EndpointPublication(
        sync_run_id=sync_run.id,
        created_by_task_id=task.id,
        created_by_task_attempt=1,
        endpoint="calendar",
        status=PublicationStatus.PUBLISHED,
        publish_sequence=1,
        adapter_version="test",
        schema_fingerprint="test",
    )
    db_session.add(publication)
    db_session.flush()
    roster = InstrumentMasterSnapshot(
        publication_id=publication.id,
        sync_run_id=sync_run.id,
        source="jquants",
        as_of_date=date(2026, 1, 9),
    )
    db_session.add(roster)
    db_session.flush()
    record = DataSnapshot(
        source="jquants",
        sync_run_id=sync_run.id,
        bar_publish_sequence=1,
        calendar_publication_id=publication.id,
        master_snapshot_id=roster.id,
        coverage_start=date(2025, 1, 6),
        coverage_end=date(2026, 1, 9),
        plan_fingerprint="publication-test",
    )
    db_session.add(record)
    db_session.commit()
    return record


@pytest.fixture()
def run(db_session, snapshot) -> ResearchRun:
    experiment = ResearchExperiment(
        definition_fingerprint=uuid.uuid4().hex,
        kind=ResearchExperimentKind.MODEL,
        data_snapshot_id=snapshot.id,
        definition={"kind": "model"},
    )
    db_session.add(experiment)
    db_session.flush()
    task = Task(task_type="model_research", status=TaskStatus.RUNNING, payload={})
    db_session.add(task)
    db_session.flush()
    research_run = ResearchRun(
        experiment_id=experiment.id,
        task_id=task.id,
        status=ResearchRunStatus.PUBLISHING,
    )
    db_session.add(research_run)
    db_session.commit()
    return research_run


def _tables() -> dict[str, pd.DataFrame]:
    return {"scores": pd.DataFrame([{"observation_date": date(2026, 1, 9), "raw_score": 0.5}])}


def _prepare(session, root, run) -> tuple[ResearchArtifactPublisher, object]:
    publisher = ResearchArtifactPublisher(session, root)
    prepared = publisher.prepare(
        run,
        tables=_tables(),
        summary={"weekly_ic_mean": 0.01},
        warnings=[],
        runtime_identity={"pyqlib_version": "0.9.7"},
    )
    session.add(publisher.artifact_row(prepared))
    run.status = ResearchRunStatus.SUCCEEDED
    session.commit()
    return publisher, prepared


def test_a_completed_publish_commits_and_becomes_readable(db_session, tmp_path, run) -> None:
    publisher, prepared = _prepare(db_session, tmp_path, run)

    publisher.commit(prepared)

    publication = db_session.scalar(
        select(ResearchArtifactPublication).where(
            ResearchArtifactPublication.research_run_id == run.id
        )
    )
    assert publication.status is ArtifactPublicationStatus.COMMITTED
    assert publication.staging_path is None
    assert (tmp_path / str(run.id)).is_dir()
    assert readable_artifact(db_session, run.id) is not None


def test_the_artifact_is_not_readable_between_the_row_and_the_rename(
    db_session, tmp_path, run
) -> None:
    """The window step 2 opens on purpose.

    The `ResearchArtifact` row is committed before the directory moves, so a
    reader that goes by the row alone would open a path that is not there. The
    publication status is what closes it.
    """
    _prepare(db_session, tmp_path, run)

    assert db_session.scalar(
        select(ResearchArtifact).where(ResearchArtifact.research_run_id == run.id)
    ) is not None
    assert not (tmp_path / str(run.id)).exists()
    assert readable_artifact(db_session, run.id) is None


def test_a_crash_before_the_rename_is_finished_by_recovery(db_session, tmp_path, run) -> None:
    """Everything the database needs was committed at step 2, so recovery moves
    the bytes forward rather than throwing the run away."""
    _prepare(db_session, tmp_path, run)
    assert (tmp_path / f"{STAGING_PREFIX}{run.id}").is_dir()

    failed = recover_publications(db_session, tmp_path)

    assert failed == []
    assert (tmp_path / str(run.id)).is_dir()
    assert readable_artifact(db_session, run.id) is not None
    db_session.refresh(run)
    assert run.status is ResearchRunStatus.SUCCEEDED


def test_a_crash_after_the_rename_is_finished_by_recovery(db_session, tmp_path, run) -> None:
    publisher, prepared = _prepare(db_session, tmp_path, run)
    # The rename lands, then the process dies before the status flips.
    publisher._move_into_place(
        db_session.get(ResearchArtifactPublication, prepared.publication_id)
    )
    db_session.commit()

    failed = recover_publications(db_session, tmp_path)

    assert failed == []
    publication = db_session.get(ResearchArtifactPublication, prepared.publication_id)
    assert publication.status is ArtifactPublicationStatus.COMMITTED
    assert readable_artifact(db_session, run.id) is not None


def test_losing_the_bytes_fails_the_run_rather_than_leaving_it_succeeded(
    db_session, tmp_path, run
) -> None:
    """The one case where recovery cannot go forward.

    Rows exist and describe an artifact, but nothing is on disk. Reporting the
    run as succeeded would leave a `ResearchArtifact` pointing at a directory
    that never existed, so the rows go with the bytes.
    """
    _prepare(db_session, tmp_path, run)
    import shutil

    shutil.rmtree(tmp_path / f"{STAGING_PREFIX}{run.id}")

    failed = recover_publications(db_session, tmp_path)

    assert failed == [run.id]
    db_session.refresh(run)
    assert run.status is ResearchRunStatus.FAILED
    assert run.error_code == "artifact_publication_lost"
    assert db_session.scalar(
        select(ResearchArtifact).where(ResearchArtifact.research_run_id == run.id)
    ) is None
    assert readable_artifact(db_session, run.id) is None


def test_recovery_is_idempotent(db_session, tmp_path, run) -> None:
    """A worker may restart repeatedly; replaying recovery must not undo it."""
    publisher, prepared = _prepare(db_session, tmp_path, run)
    publisher.commit(prepared)

    assert recover_publications(db_session, tmp_path) == []
    assert recover_publications(db_session, tmp_path) == []
    assert readable_artifact(db_session, run.id) is not None


def test_orphan_staging_from_a_crash_before_step_two_is_swept(db_session, tmp_path, run) -> None:
    """Bytes written but never recorded.

    No publication row mentions this directory, so nothing will ever publish it
    and nothing will ever find it again — it would sit there until the disk
    budget tripped.
    """
    orphan = tmp_path / f"{STAGING_PREFIX}{uuid.uuid4()}"
    orphan.mkdir(parents=True)
    (orphan / "scores.parquet").write_bytes(b"stale")

    recover_publications(db_session, tmp_path)

    assert not orphan.exists()


def test_publishing_twice_over_a_committed_artifact_is_refused(db_session, tmp_path, run) -> None:
    publisher, prepared = _prepare(db_session, tmp_path, run)
    publisher.commit(prepared)

    with pytest.raises(PublicationError, match="already published"):
        publisher.prepare(
            run,
            tables=_tables(),
            summary={},
            warnings=[],
            runtime_identity={},
        )


def test_abandoning_a_prepared_publication_removes_its_bytes(db_session, tmp_path, run) -> None:
    publisher, prepared = _prepare(db_session, tmp_path, run)

    publisher.abandon(prepared, "cancelled during publishing")

    publication = db_session.get(ResearchArtifactPublication, prepared.publication_id)
    assert publication.status is ArtifactPublicationStatus.FAILED
    assert not (tmp_path / f"{STAGING_PREFIX}{run.id}").exists()
    assert not (tmp_path / str(run.id)).exists()
