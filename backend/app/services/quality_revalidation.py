"""Re-validation: today's quality rules, yesterday's data.

Rules change. Thresholds will become editable (ticket 14). Without this, the
only way to re-judge two years of stored bars against a changed rule set is to
re-fetch them, which takes about fourteen hours and asks J-Quants for data it
already gave us.

Three decisions shape everything here:

1. **It acts on the head snapshot, never on a snapshot you pick.** There is no
   "which snapshot" question in the model — the answer is always the latest —
   and inventing one would add a layer of choice with nothing behind it.
2. **It writes a new snapshot; it never edits the old one.** A verdict is
   decided once and frozen (04), and a snapshot a backtest has already bound to
   must not have its answer changed underneath it. Keeping both is also what
   makes "what did the rule change actually do" a question you can answer.
   The new snapshot inherits cutoff, calendar and master, so the head moving
   forward is not a step backwards: same data, newer judgement.
3. **A new snapshot every time, even when nothing changed.** Deciding that two
   evaluations "reached the same conclusion" means deciding whether identical
   findings from changed rule code count as the same — every definition of that
   lies at some boundary. The cost of not deciding is one row.

It is queued work with its own task type rather than a mode of `jquants_sync`:
the sync's active-run guard would otherwise count re-validations and block
syncing, and `SyncRun`'s state machine describes phases this has none of. And
it cannot run inside the HTTP request — the pass is several aggregates over two
million rows.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models.market_data import (
    DataSnapshot,
    DataSnapshotHead,
    QualityEvaluation,
    QualityEvaluationKind,
    QualityEvaluationStatus,
)
from app.models.task import Task
from app.services import quality_pass, source_state
from app.services.calendar_port import DbCalendarPort
from app.services.quality_rules import QualityPolicy, policy_snapshot

TASK_TYPE = "quality_revalidation"


class RevalidationConflict(RuntimeError):
    """The source is busy. Carries what is holding it, so the caller can say so."""

    def __init__(self, message: str, *, active: dict[str, Any]) -> None:
        super().__init__(message)
        self.active = active


class RevalidationUnavailable(RuntimeError):
    """There is nothing to re-validate yet."""


@dataclass(frozen=True)
class EvaluationView:
    id: uuid.UUID
    kind: QualityEvaluationKind
    source: str
    status: QualityEvaluationStatus
    produced_snapshot_id: uuid.UUID | None
    error_summary: str | None
    created_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "kind": self.kind.value,
            "source": self.source,
            "status": self.status.value,
            "produced_snapshot_id": (
                str(self.produced_snapshot_id) if self.produced_snapshot_id else None
            ),
            "error_summary": self.error_summary,
            "created_at": self.created_at,
        }


def view(evaluation: QualityEvaluation) -> EvaluationView:
    return EvaluationView(
        id=evaluation.id,
        kind=evaluation.kind,
        source=evaluation.source,
        status=evaluation.status,
        produced_snapshot_id=evaluation.produced_snapshot_id,
        error_summary=evaluation.error_summary,
        created_at=evaluation.created_at,
    )


class QualityRevalidationWorkflow:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        quality_policy: QualityPolicy = QualityPolicy(),
        source: str = "jquants",
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._sessions = session_factory
        self._quality_policy = quality_policy
        self._source = source
        self._now = now

    # ---------------------------------------------------------------- commands

    def start(self) -> EvaluationView:
        """Queue a re-validation of the current head, or refuse and say why.

        Refuses while anything else holds the source. The reason is not
        concurrency — one worker runs everything serially — it is that a
        re-validation names its subject when it *runs*, not when it is queued:
        let one sit behind a sync and it will judge whatever snapshot that sync
        produced, which is not the snapshot the person was looking at.
        """
        with self._sessions() as session:
            source_state.lock_source(session, self._source)

            run = source_state.active_sync_run(session, self._source)
            if run is not None:
                raise RevalidationConflict(
                    "A sync is running for this source; re-validation would judge its result "
                    "rather than the snapshot you are looking at",
                    active={"kind": "sync", "sync_run_id": str(run.id), "status": run.status.value},
                )
            running = source_state.active_revalidation(session, self._source)
            if running is not None:
                raise RevalidationConflict(
                    "A re-validation is already queued for this source",
                    active={
                        "kind": "revalidate",
                        "evaluation_id": str(running.id),
                        "status": running.status.value,
                    },
                )

            head = session.get(DataSnapshotHead, self._source)
            if head is None:
                raise RevalidationUnavailable("There is no snapshot to re-validate yet")

            task = Task(task_type=TASK_TYPE, payload={}, progress={})
            session.add(task)
            session.flush()
            evaluation = QualityEvaluation(
                kind=QualityEvaluationKind.REVALIDATE,
                source=self._source,
                task_id=task.id,
                status=QualityEvaluationStatus.QUEUED,
                policy=policy_snapshot(self._quality_policy),
            )
            session.add(evaluation)
            session.flush()
            task.payload = {"evaluation_id": str(evaluation.id)}
            session.commit()
            return view(evaluation)

    def latest(self) -> EvaluationView | None:
        """The most recent re-validation, which is what the UI reports on.

        Sync evaluations are left out: they are part of a run the UI already
        reports in full, and surfacing them here would put a second, competing
        progress line next to it.
        """
        with self._sessions() as session:
            evaluation = session.scalars(
                select(QualityEvaluation)
                .where(
                    QualityEvaluation.source == self._source,
                    QualityEvaluation.kind == QualityEvaluationKind.REVALIDATE,
                )
                .order_by(QualityEvaluation.created_at.desc())
                .limit(1)
            ).first()
            return view(evaluation) if evaluation else None

    # --------------------------------------------------------------- execution

    def execute(self, evaluation_id: uuid.UUID) -> dict[str, Any]:
        """Re-judge the head and publish the result as a new snapshot."""
        subject_id = self._begin(evaluation_id)
        try:
            outcome = self._evaluate(evaluation_id, subject_id)
            return self._activate(evaluation_id, subject_id, outcome)
        except Exception as exc:
            self._fail(evaluation_id, exc)
            raise

    def _begin(self, evaluation_id: uuid.UUID) -> uuid.UUID:
        """Mark the evaluation running and pin what it is judging.

        The subject is read once, here, and checked again at activation. The
        start guard already keeps a sync from overlapping; this makes the
        assumption explicit rather than relying on it from a distance.
        """
        with self._sessions() as session:
            evaluation = session.get(QualityEvaluation, evaluation_id, with_for_update=True)
            if evaluation is None:
                raise LookupError("Quality evaluation not found")
            head = session.get(DataSnapshotHead, self._source)
            if head is None:
                raise RevalidationUnavailable("There is no snapshot to re-validate")
            evaluation.status = QualityEvaluationStatus.RUNNING
            evaluation.error_summary = None
            session.commit()
            return head.snapshot_id

    def _evaluate(
        self, evaluation_id: uuid.UUID, subject_id: uuid.UUID
    ) -> quality_pass.QualityOutcome:
        with self._sessions() as session:
            subject = session.get(DataSnapshot, subject_id)
            calendar_publication_id = subject.calendar_publication_id
            scope = quality_pass.SnapshotScope(
                source=subject.source, bar_publish_sequence=subject.bar_publish_sequence
            )

        calendar = DbCalendarPort(self._sessions, calendar_publication_id)
        with self._sessions() as session:
            outcome = quality_pass.evaluate(
                session,
                evaluation_id=evaluation_id,
                scope=scope,
                source=self._source,
                calendar=calendar,
                calendar_publication_id=calendar_publication_id,
                policy=self._quality_policy,
            )
            session.commit()
        return outcome

    def _activate(
        self, evaluation_id: uuid.UUID, subject_id: uuid.UUID, outcome: quality_pass.QualityOutcome
    ) -> dict[str, Any]:
        """One transaction: the new snapshot, the head, and the verdict record."""
        with self._sessions() as session:
            source_state.lock_source(session, self._source)
            evaluation = session.get(QualityEvaluation, evaluation_id, with_for_update=True)
            head = session.get(DataSnapshotHead, self._source, with_for_update=True)
            if head is None or head.snapshot_id != subject_id:
                # Publishing now would attach a verdict to data that is no
                # longer what was judged. Failing is the only honest option.
                raise RevalidationUnavailable(
                    "The head snapshot changed while the re-validation was running"
                )
            subject = session.get(DataSnapshot, subject_id)

            snapshot = DataSnapshot(
                source=self._source,
                # No run and no mode: nothing was fetched. Inheriting either
                # would put a claim in the row that no request backs up.
                sync_run_id=None,
                mode=None,
                bar_publish_sequence=subject.bar_publish_sequence,
                calendar_publication_id=subject.calendar_publication_id,
                master_snapshot_id=subject.master_snapshot_id,
                coverage_start=subject.coverage_start,
                coverage_end=subject.coverage_end,
                # `verified_*` means "re-checked at the source in this pass",
                # and nothing was. Copying the subject's window would claim a
                # request that never happened.
                verified_start=None,
                verified_end=None,
                plan_fingerprint=subject.plan_fingerprint,
                is_backtest_eligible=outcome.is_backtest_eligible,
                version=source_state.next_snapshot_version(session, self._source),
            )
            session.add(snapshot)
            session.flush()
            source_state.set_head(session, head, self._source, snapshot.id, self._now())

            evaluation.produced_snapshot_id = snapshot.id
            evaluation.status = QualityEvaluationStatus.SUCCEEDED
            session.commit()

            return {
                "evaluation_id": str(evaluation_id),
                "snapshot_id": str(snapshot.id),
                "version": snapshot.version,
                "revalidated_snapshot_id": str(subject_id),
                "findings": outcome.findings,
                "is_backtest_eligible": outcome.is_backtest_eligible,
            }

    def _fail(self, evaluation_id: uuid.UUID, exc: Exception) -> None:
        """Record that the pass produced no verdict, then let the task fail.

        Deliberately not a verdict of its own: an evaluator that crashed said
        neither "the data is fine" nor "the data is bad", and writing an
        ineligible snapshot would be indistinguishable from having checked.
        The previous snapshot stays head.
        """
        with self._sessions() as session:
            evaluation = session.get(QualityEvaluation, evaluation_id, with_for_update=True)
            if evaluation is None:
                return
            evaluation.status = QualityEvaluationStatus.FAILED
            evaluation.error_summary = str(exc)
            session.commit()
