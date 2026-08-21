"""Continuous batch sync orchestration (docs/design/jquants-continuous-batch-sync.md).

This is the deep module the design calls for: HTTP and the worker cross only
`start / execute / inspect / request_cancel / resume`. Neither knows about the
date planner, the batch loop, publication attempts, or checkpoint recovery.

Two invariants drive most of the code below:

1. The atomic unit is one publication generation, not the whole run. A batch
   either becomes official market fact with a publish sequence, or changes
   nothing at all.
2. A SyncRun and its Task reach terminal state in the *same* transaction, so
   "succeeded run with a RUNNING task" is never a committed state.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.integrations.jquants import FetchResult, JQuantsAdapter, JQuantsError
from app.models.market_data import (
    TERMINAL_RUN_STATUSES,
    BarObservationDisposition,
    BarRecord,
    BarVersion,
    CurrentBar,
    DataSnapshot,
    DataSnapshotHead,
    EndpointPublication,
    Instrument,
    InstrumentMasterSnapshot,
    InstrumentMasterSnapshotMember,
    PublicationBarObservation,
    PublicationStatus,
    QualityEvaluation,
    QualityEvaluationKind,
    QualityEvaluationStatus,
    RawSourcePage,
    SyncBatch,
    SyncBatchStatus,
    SyncMode,
    SyncPhase,
    SyncRun,
    SyncRunStatus,
    SyncTargetDate,
    SyncTargetStatus,
    TradingCalendar,
    publish_sequence_seq,
)
from app.models.task import Task, TaskStatus
from app.services import quality_pass, snapshot_reader, source_state
from app.services.calendar_normalization import MARKET_TSE, CalendarDay, normalize_calendar
from app.services.calendar_port import DbCalendarPort
from app.services.quality_rules import QualityPolicy, evaluate_row_local, policy_snapshot
from app.services.sync_planner import (
    choose_target_dates,
    chunk_target_dates,
    plan_fingerprint,
)

CALENDAR_ENDPOINT = "markets/calendar"
BARS_ENDPOINT = "equities/bars/daily"
MASTER_ENDPOINT = "equities/master"

RAW_PAGE_RETENTION = timedelta(days=90)


class SyncCancelled(RuntimeError):
    """Cooperative cancellation reached a safe point."""


class SyncInvariantError(RuntimeError):
    """Persisted state contradicts itself; automatic continuation is unsafe."""


class SyncConflict(RuntimeError):
    """The requested transition does not apply to the run's current state."""


@dataclass(frozen=True)
class SyncNow:
    """The ordinary user command. Deliberately carries no date range or batch
    size — those are policy, not interface (§4)."""


@dataclass(frozen=True)
class SyncPolicy:
    batch_size: int = 5
    max_task_attempts: int = 3


@dataclass(frozen=True)
class SyncOutcome:
    run_id: uuid.UUID
    status: SyncRunStatus
    mode: SyncMode | None
    snapshot_id: uuid.UUID | None
    coverage_start: date | None
    coverage_end: date | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sync_run_id": str(self.run_id),
            "status": self.status.value,
            "mode": self.mode.value if self.mode else None,
            "snapshot_id": str(self.snapshot_id) if self.snapshot_id else None,
            "coverage_start": self.coverage_start.isoformat() if self.coverage_start else None,
            "coverage_end": self.coverage_end.isoformat() if self.coverage_end else None,
        }


@dataclass(frozen=True)
class SyncRunView:
    id: uuid.UUID
    task_id: uuid.UUID
    source: str
    status: SyncRunStatus
    phase: SyncPhase
    mode: SyncMode | None
    task_attempt: int
    target_dates: int
    processed_dates: int
    total_batches: int
    completed_batches: int
    current_batch: int | None
    coverage_before: date | None
    planned_start: date | None
    planned_end: date | None
    rows_received: int
    rows_new: int
    rows_changed: int
    rows_unchanged: int
    pages_received: int
    actual_min: date | None
    actual_max: date | None
    snapshot_id: uuid.UUID | None
    resumable: bool
    current_date: date | None
    error_code: str | None
    error_summary: str | None
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    batches: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "task_id": str(self.task_id),
            "source": self.source,
            "status": self.status.value,
            "phase": self.phase.value,
            "mode": self.mode.value if self.mode else None,
            "task_attempt": self.task_attempt,
            "target_dates": self.target_dates,
            "processed_dates": self.processed_dates,
            "total_batches": self.total_batches,
            "completed_batches": self.completed_batches,
            "current_batch": self.current_batch,
            "current_date": self.current_date,
            "coverage_before": self.coverage_before,
            "coverage_current": self.actual_max,
            "planned_start": self.planned_start,
            "planned_end": self.planned_end,
            "rows_received": self.rows_received,
            "rows_new": self.rows_new,
            "rows_changed": self.rows_changed,
            "rows_unchanged": self.rows_unchanged,
            "pages_received": self.pages_received,
            "actual_min": self.actual_min,
            "actual_max": self.actual_max,
            "snapshot_id": str(self.snapshot_id) if self.snapshot_id else None,
            "resumable": self.resumable,
            # Flat, so this matches the shape the list endpoint returns —
            # the UI polls both and must not special-case one of them.
            "error_code": self.error_code,
            "error_summary": self.error_summary,
            "error_retryable": _is_retryable(self.error_code),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "batches": self.batches,
        }


RETRYABLE_ERROR_CODES = frozenset({"source_unavailable", "source_timeout", "worker_restart"})

NON_RESUMABLE_ERROR_CODES = frozenset(
    {"empty_initial_bars", "empty_full_reconcile_plan", "sync_invariant"}
)


def _is_retryable(error_code: str | None) -> bool:
    return error_code in RETRYABLE_ERROR_CODES


class JQuantsSyncWorkflow:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        adapter: JQuantsAdapter | None = None,
        *,
        policy: SyncPolicy = SyncPolicy(),
        quality_policy: QualityPolicy = QualityPolicy(),
        source: str = "jquants",
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._sessions = session_factory
        self._adapter = adapter
        self._policy = policy
        self._quality_policy = quality_policy
        self._source = source
        self._now = now

    # ---------------------------------------------------------------- commands

    def start(self, command: SyncNow = SyncNow(), *, idempotency_key: str | None = None) -> SyncRunView:
        """Create the one-to-one SyncRun + Task under the source lock.

        Returns the existing run when one is already active or when the same
        idempotency key was used before — never a second active run. Refuses
        outright while a re-validation holds the source: that is the other half
        of the exclusion re-validation asks for, and queueing behind it would
        leave two things claiming the source at once.
        """
        with self._sessions() as session:
            source_state.lock_source(session, self._source)

            if idempotency_key:
                existing = session.scalar(
                    select(SyncRun).where(
                        SyncRun.source == self._source,
                        SyncRun.idempotency_key == idempotency_key,
                    )
                )
                if existing:
                    return self._view(session, existing)

            active = source_state.active_sync_run(session, self._source)
            if active:
                return self._view(session, active)

            if source_state.active_revalidation(session, self._source) is not None:
                raise SyncConflict(
                    "A re-validation is running for this source; sync is unavailable "
                    "until it finishes"
                )

            task = Task(task_type="jquants_sync", payload={}, progress={})
            session.add(task)
            session.flush()
            run = SyncRun(
                task_id=task.id,
                source=self._source,
                status=SyncRunStatus.QUEUED,
                phase=SyncPhase.DISCOVERING_CALENDAR,
                idempotency_key=idempotency_key,
                batch_size=self._policy.batch_size,
            )
            session.add(run)
            session.flush()
            task.payload = {"sync_run_id": str(run.id)}
            session.commit()
            return self._view(session, run)

    def inspect(self, run_id: uuid.UUID) -> SyncRunView:
        with self._sessions() as session:
            run = session.get(SyncRun, run_id)
            if run is None:
                raise LookupError("Sync run not found")
            return self._view(session, run, include_batches=True)

    def request_cancel(self, run_id: uuid.UUID) -> SyncRunView:
        """Request only. A RUNNING worker confirms the cancellation once it
        reaches a safe point (§13.2)."""
        with self._sessions() as session:
            run, task = _lock_run_and_task(session, run_id)
            if run.status in TERMINAL_RUN_STATUSES:
                raise SyncConflict("Sync run is already terminal")

            if run.status == SyncRunStatus.QUEUED:
                # Never claimed, so nothing has to unwind: cancel both atomically.
                _terminate(run, task, SyncRunStatus.CANCELLED, self._now())
                run.cancel_requested_at = self._now()
            else:
                run.status = SyncRunStatus.CANCELLING
                run.cancel_requested_at = self._now()
            session.commit()
            return self._view(session, run)

    def resume(self, run_id: uuid.UUID) -> SyncRunView:
        """Re-queue the *same* Task against the persisted checkpoint (§13.3)."""
        with self._sessions() as session:
            source_state.lock_source(session, self._source)
            run, task = _lock_run_and_task(session, run_id)

            if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                raise SyncConflict("Sync run is already queued or running")
            if not is_resumable(session, run, task, self._policy):
                raise SyncConflict("Sync run is not resumable")

            self._validate_calendar_checkpoint(session, run)

            if run.plan_fingerprint:
                _reset_unpublished_batches(session, run)

            task.status = TaskStatus.QUEUED
            task.error = None
            task.started_at = None
            task.finished_at = None

            run.status = SyncRunStatus.QUEUED
            run.cancel_requested_at = None
            run.finished_at = None
            run.error_code = None
            run.error_summary = None
            session.commit()
            return self._view(session, run)

    def force_retry(self, run_id: uuid.UUID) -> SyncRunView:
        """Revive a run the worker gave up on, attempt counter and all.

        The counter records how many times a worker *picked the task up*, not
        how many times anything failed — a graceful restart consumes one just
        as a crash does — so a run can reach the cap with nothing wrong with
        it. Deliberately a separate command from `resume`: continuing a run is
        routine, overturning the verdict the system already reached is not, and
        the caller should have looked at why before doing it.

        There is no second cap. A human confirming each time is the throttle;
        a number on top of that would only build the dead end again.
        """
        with self._sessions() as session:
            source_state.lock_source(session, self._source)
            run, task = _lock_run_and_task(session, run_id)

            if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                raise SyncConflict("Sync run is already queued or running")
            if run.status in {SyncRunStatus.SUCCEEDED, SyncRunStatus.NO_CHANGE}:
                # Re-judging finished data is re-validation's job (ticket 16).
                raise SyncConflict("A completed run cannot be retried; re-validate instead")

            self._validate_calendar_checkpoint(session, run)
            if run.plan_fingerprint:
                _reset_unpublished_batches(session, run)

            # Both halves together: a terminal run with a cleared counter is a
            # state nothing else in the system is written against.
            task.attempt_count = 0
            task.status = TaskStatus.QUEUED
            task.error = None
            task.started_at = None
            task.finished_at = None

            run.status = SyncRunStatus.QUEUED
            run.cancel_requested_at = None
            run.finished_at = None
            run.error_code = None
            run.error_summary = None
            session.commit()
            return self._view(session, run)

    # ---------------------------------------------------------------- execution

    def execute(self, run_id: uuid.UUID) -> SyncOutcome:
        """Run to completion. Called by the single worker, synchronously.

        Idempotent per run_id: everything already published is skipped, and the
        frozen plan is never recomputed.
        """
        if self._adapter is None:
            raise ValueError("A J-Quants adapter is required to execute a sync run")

        self._begin(run_id)
        try:
            plan = self._ensure_plan(run_id)
            self._execute_batches(run_id, plan)
            return self._complete(run_id, plan)
        except SyncCancelled:
            self._confirm_cancellation(run_id)
            raise
        except Exception as exc:
            self._fail(run_id, exc)
            raise

    def _begin(self, run_id: uuid.UUID) -> None:
        with self._sessions() as session:
            run, task = _lock_run_and_task(session, run_id)
            if run.status in TERMINAL_RUN_STATUSES:
                raise SyncConflict("Sync run is already terminal")
            if run.cancel_requested_at is not None:
                raise SyncCancelled("Cancellation was requested before execution started")
            run.status = SyncRunStatus.RUNNING
            run.started_at = run.started_at or self._now()
            run.error_code = None
            run.error_summary = None
            if run.plan_fingerprint is None:
                run.phase = SyncPhase.DISCOVERING_CALENDAR
            elif run.phase in {SyncPhase.DISCOVERING_CALENDAR, SyncPhase.PLANNING}:
                run.phase = SyncPhase.BARS
            task.status = TaskStatus.RUNNING
            task.started_at = task.started_at or self._now()
            session.commit()

    # ------------------------------------------------------------ calendar/plan

    def _ensure_plan(self, run_id: uuid.UUID) -> _FrozenPlan:
        """Return the frozen plan, discovering the calendar first if needed.

        Once `plan_fingerprint` exists the calendar is never requested again —
        dates that appear mid-run belong to the next `sync now` (§10.2).
        """
        with self._sessions() as session:
            run = session.get(SyncRun, run_id)
            self._validate_calendar_checkpoint(session, run)
            if run.plan_fingerprint:
                return _load_frozen_plan(session, run)
            published_calendar = _calendar_publication(session, run_id, PublicationStatus.PUBLISHED)
            legacy_rebuild = published_calendar is not None

        if legacy_rebuild:
            return self._freeze_plan_from_raw_pages(run_id)
        return self._discover_calendar_and_freeze(run_id)

    def _discover_calendar_and_freeze(self, run_id: uuid.UUID) -> _FrozenPlan:
        self._check_cancelled(run_id)

        # A STAGING attempt cannot be continued in place: there is no way to
        # prove every page arrived, nor which page_index comes next.
        with self._sessions() as session:
            run = session.get(SyncRun, run_id)
            stale = _calendar_publication(session, run_id, PublicationStatus.STAGING)
            if stale is not None:
                stale.status = PublicationStatus.FAILED
                stale.error_code = "abandoned_attempt"
                stale.error_summary = "Superseded by a later calendar attempt"
                session.commit()

        publication_id = self._begin_publication(run_id, CALENDAR_ENDPOINT, {}, scope_ordinal=0)
        result = self._fetch(lambda: self._adapter.fetch_calendar(), "calendar")
        self._store_pages(publication_id, result.pages)
        return self._freeze_plan(run_id, publication_id, normalize_calendar(result.rows), result)

    def _freeze_plan_from_raw_pages(self, run_id: uuid.UUID) -> _FrozenPlan:
        """Legacy compatibility only: a PUBLISHED calendar with no plan.

        The stored raw pages *are* that run's date-discovery fact, so the plan
        is rebuilt from them rather than re-requesting the calendar, which
        would silently change what the run means.
        """
        with self._sessions() as session:
            publication = _calendar_publication(session, run_id, PublicationStatus.PUBLISHED)
            pages = session.scalars(
                select(RawSourcePage)
                .where(RawSourcePage.publication_id == publication.id)
                .order_by(RawSourcePage.page_index)
            ).all()
            if not pages:
                raise SyncInvariantError(
                    "Calendar is published but its raw pages are missing; cannot rebuild the plan"
                )
            rows: list[dict[str, Any]] = []
            for index, page in enumerate(pages):
                if page.page_index != index:
                    raise SyncInvariantError("Calendar raw pages are not contiguous; cannot rebuild the plan")
                data = page.payload.get("data")
                if not isinstance(data, list):
                    raise SyncInvariantError("Calendar raw page payload is unreadable; cannot rebuild the plan")
                rows.extend(data)
            publication_id = publication.id
        return self._freeze_plan(run_id, publication_id, normalize_calendar(rows), None)

    def _freeze_plan(
        self,
        run_id: uuid.UUID,
        calendar_publication_id: uuid.UUID,
        calendar: list[CalendarDay],
        result: FetchResult | None,
    ) -> _FrozenPlan:
        """Publish the calendar, normalise it and write the complete plan in one
        transaction.

        This is what makes "calendar published but plan missing" and "half a
        plan" unreachable for new runs (§10.1). The normalised calendar rows
        join that guarantee: a published calendar publication always has the
        complete set of days a snapshot will later resolve through it.
        """
        visible_dates = [day.trade_date for day in calendar if day.is_open]
        hol_div_by_date = {day.trade_date: day.hol_div for day in calendar}

        with self._sessions() as session:
            run, task = _lock_run_and_task(session, run_id)
            _require_active(run, task)

            publication = session.get(EndpointPublication, calendar_publication_id, with_for_update=True)
            coverage_before, last_full_at = _snapshot_watermarks(session, self._source)
            plan = choose_target_dates(
                visible_dates,
                snapshot_coverage_end=coverage_before,
                last_full_snapshot_at=last_full_at,
                now=self._now(),
            )
            fingerprint = plan_fingerprint(plan.target_dates)
            chunks = chunk_target_dates(plan.target_dates, batch_size=run.batch_size)

            if publication.status != PublicationStatus.PUBLISHED:
                publication.status = PublicationStatus.PUBLISHED
                publication.published_at = self._now()
                publication.publish_sequence = _next_publish_sequence(session)
                if result is not None:
                    publication.schema_fingerprint = _schema_fingerprint(result.rows)
                    publication.stats = {"pages": result.page_count, "rows": len(result.rows)}

            # Guarded by existence rather than by the publish branch above: the
            # legacy rebuild path arrives with an already-PUBLISHED calendar,
            # and leaving it without rows would give its snapshot a calendar
            # pointer that resolves to nothing.
            already_written = session.scalar(
                select(func.count())
                .select_from(TradingCalendar)
                .where(TradingCalendar.publication_id == calendar_publication_id)
            )
            if not already_written:
                session.add_all(
                    TradingCalendar(
                        publication_id=calendar_publication_id,
                        market=MARKET_TSE,
                        trade_date=day.trade_date,
                        is_open=day.is_open,
                        session=day.session,
                        hol_div=day.hol_div,
                    )
                    for day in calendar
                )

            for ordinal, dates in enumerate(chunks):
                batch = SyncBatch(
                    sync_run_id=run_id,
                    ordinal=ordinal,
                    status=SyncBatchStatus.PENDING,
                    target_start=dates[0],
                    target_end=dates[-1],
                    target_dates=len(dates),
                )
                session.add(batch)
                session.flush()
                session.add_all(
                    SyncTargetDate(
                        sync_run_id=run_id,
                        sync_batch_id=batch.id,
                        trade_date=item,
                        source_calendar_code=hol_div_by_date[item],
                        status=SyncTargetStatus.PENDING,
                    )
                    for item in dates
                )

            run.mode = plan.mode
            run.coverage_before = coverage_before
            run.planned_start = plan.target_dates[0] if plan.target_dates else None
            run.planned_end = plan.target_dates[-1] if plan.target_dates else None
            run.plan_fingerprint = fingerprint
            run.target_dates = len(plan.target_dates)
            run.total_batches = len(chunks)
            run.phase = SyncPhase.BARS
            session.commit()

            return _FrozenPlan(
                mode=plan.mode,
                fingerprint=fingerprint,
                target_dates=list(plan.target_dates),
                calendar_publication_id=calendar_publication_id,
            )

    def _validate_calendar_checkpoint(self, session: Session, run: SyncRun) -> None:
        """Reject the state combinations §10.2 marks as unsafe to continue."""
        published = _calendar_publication(session, run.id, PublicationStatus.PUBLISHED)
        target_count = session.scalar(
            select(func.count()).select_from(SyncTargetDate).where(SyncTargetDate.sync_run_id == run.id)
        )

        if run.plan_fingerprint is None:
            if target_count:
                raise SyncInvariantError("Sync run has target dates but no frozen plan fingerprint")
            return

        if published is None:
            raise SyncInvariantError("Sync run has a frozen plan but no published calendar publication")
        if target_count != run.target_dates:
            raise SyncInvariantError("Frozen plan is incomplete; refusing to continue automatically")

        dates = session.scalars(
            select(SyncTargetDate.trade_date)
            .where(SyncTargetDate.sync_run_id == run.id)
            .order_by(SyncTargetDate.trade_date)
        ).all()
        if plan_fingerprint(list(dates)) != run.plan_fingerprint:
            raise SyncInvariantError("Frozen plan fingerprint does not match its target dates")

    # ----------------------------------------------------------------- batches

    def _execute_batches(self, run_id: uuid.UUID, plan: _FrozenPlan) -> None:
        while True:
            with self._sessions() as session:
                batch = session.scalars(
                    select(SyncBatch)
                    .where(
                        SyncBatch.sync_run_id == run_id,
                        SyncBatch.status != SyncBatchStatus.PUBLISHED,
                    )
                    .order_by(SyncBatch.ordinal)
                    .limit(1)
                ).first()
                if batch is None:
                    return
                batch_id, ordinal = batch.id, batch.ordinal
                dates = session.scalars(
                    select(SyncTargetDate.trade_date)
                    .where(SyncTargetDate.sync_batch_id == batch_id)
                    .order_by(SyncTargetDate.trade_date)
                ).all()

            self._check_cancelled(run_id)
            self._execute_batch(run_id, batch_id, ordinal, list(dates))

    def _execute_batch(
        self, run_id: uuid.UUID, batch_id: uuid.UUID, ordinal: int, dates: list[date]
    ) -> None:
        with self._sessions() as session:
            batch = session.get(SyncBatch, batch_id, with_for_update=True)
            batch.status = SyncBatchStatus.STAGING
            batch.attempt_count += 1
            batch.started_at = batch.started_at or self._now()
            batch.error_code = None
            batch.error_summary = None
            attempt = batch.attempt_count
            run = session.get(SyncRun, run_id)
            run.current_batch = ordinal
            session.execute(
                SyncTargetDate.__table__.update()
                .where(
                    SyncTargetDate.sync_batch_id == batch_id,
                    SyncTargetDate.status != SyncTargetStatus.PUBLISHED,
                )
                .values(status=SyncTargetStatus.STAGING)
            )
            session.commit()

        publication_id = self._begin_publication(
            run_id,
            BARS_ENDPOINT,
            {"selection": "date", "start": str(dates[0]), "end": str(dates[-1])},
            scope_ordinal=ordinal,
            batch_id=batch_id,
            attempt=attempt,
        )

        try:
            page_index = 0
            pages_received = 0
            rows_received = 0
            for trade_date in dates:
                self._check_cancelled(run_id)
                result = self._fetch(
                    lambda d=trade_date: self._adapter.fetch_daily_bars(d.isoformat()), "daily bars"
                )
                self._store_pages(publication_id, result.pages, start_index=page_index)
                page_index += result.page_count
                pages_received += result.page_count
                rows_received += len(result.rows)
                self._observe_rows(publication_id, trade_date, result.rows)

            self._check_cancelled(run_id)
            self._publish_batch(run_id, batch_id, publication_id, pages_received, rows_received)
        except SyncCancelled:
            self._abandon_publication(
                publication_id, batch_id, "cancelled", "Cancelled before publication", PublicationStatus.CANCELLED
            )
            raise
        except Exception as exc:
            self._abandon_publication(
                publication_id, batch_id, _error_code(exc), str(exc), PublicationStatus.FAILED
            )
            raise

    def _observe_rows(
        self, publication_id: uuid.UUID, trade_date: date, rows: list[dict[str, Any]]
    ) -> None:
        """Record what this publication saw — every returned row, UNCHANGED
        included, so the snapshot can resolve versions by sequence alone."""
        # Every row here shares `trade_date` (enforced below), so the business
        # key reduces to the bar record. Checked while both rows are still in
        # hand: the observation upsert further down is keyed by
        # (publication, bar record) and would quietly keep only the last one.
        seen: set[uuid.UUID] = set()
        with self._sessions() as session:
            for row in rows:
                code = str(_required(row, "Code"))
                row_date = date.fromisoformat(str(_required(row, "Date")))
                if row_date != trade_date:
                    raise ValueError(
                        f"J-Quants returned {row_date} for a {trade_date} daily bars request"
                    )
                instrument = _instrument(session, self._source, code)
                record = _bar_record(session, self._source, instrument.instrument_id, row_date)
                if record.id in seen:
                    raise ValueError(
                        f"J-Quants returned {code} twice for {row_date}; the batch contradicts itself"
                    )
                seen.add(record.id)
                values = _bar_values(row)
                content_hash = _hash(values)

                existing_version = session.scalar(
                    select(BarVersion).where(
                        BarVersion.bar_record_id == record.id,
                        BarVersion.content_hash == content_hash,
                    )
                )
                current = session.get(CurrentBar, record.id)
                current_hash = (
                    session.get(BarVersion, current.bar_version_id).content_hash if current else None
                )

                if existing_version is not None:
                    version = existing_version
                    version.last_seen_at = self._now()
                else:
                    # Evaluated here, once, because the verdict is a pure
                    # function of `values` — the same content reaching us again
                    # reuses the row above and carries this answer with it.
                    quality_status, quality_rules = evaluate_row_local(
                        values, self._quality_policy
                    )
                    version = BarVersion(
                        bar_record_id=record.id,
                        content_hash=content_hash,
                        quality_status=quality_status,
                        quality_rules=quality_rules,
                        **values,
                    )
                    session.add(version)
                    session.flush()

                if current is None:
                    disposition = BarObservationDisposition.NEW
                elif current_hash == content_hash:
                    disposition = BarObservationDisposition.UNCHANGED
                elif existing_version is not None:
                    disposition = BarObservationDisposition.REVERTED
                else:
                    disposition = BarObservationDisposition.CHANGED

                session.merge(
                    PublicationBarObservation(
                        publication_id=publication_id,
                        bar_record_id=record.id,
                        bar_version_id=version.id,
                        disposition=disposition,
                        observed_at=self._now(),
                    )
                )
            session.commit()

    def _publish_batch(
        self,
        run_id: uuid.UUID,
        batch_id: uuid.UUID,
        publication_id: uuid.UUID,
        pages_received: int,
        rows_received: int,
    ) -> None:
        """The atomic step. Everything before this changed nothing official."""
        with self._sessions() as session:
            run, task = _lock_run_and_task(session, run_id)
            batch = session.get(SyncBatch, batch_id, with_for_update=True)
            publication = session.get(EndpointPublication, publication_id, with_for_update=True)
            _require_active(run, task)
            if batch.status == SyncBatchStatus.PUBLISHED:
                return
            if publication.status != PublicationStatus.STAGING:
                raise SyncInvariantError("Bars publication is no longer staging; refusing to publish")

            sequence = _next_publish_sequence(session)
            observations = session.scalars(
                select(PublicationBarObservation).where(
                    PublicationBarObservation.publication_id == publication_id
                )
            ).all()

            for observation in observations:
                current = session.get(CurrentBar, observation.bar_record_id)
                if current is None:
                    session.add(
                        CurrentBar(
                            bar_record_id=observation.bar_record_id,
                            bar_version_id=observation.bar_version_id,
                            publication_id=publication_id,
                            publish_sequence=sequence,
                            updated_at=self._now(),
                        )
                    )
                elif sequence > current.publish_sequence:
                    current.bar_version_id = observation.bar_version_id
                    current.publication_id = publication_id
                    current.publish_sequence = sequence
                    current.updated_at = self._now()

            counts = _disposition_counts(observations)
            publication.status = PublicationStatus.PUBLISHED
            publication.publish_sequence = sequence
            publication.published_at = self._now()
            publication.stats = {"pages": pages_received, "rows": rows_received}

            session.execute(
                SyncTargetDate.__table__.update()
                .where(SyncTargetDate.sync_batch_id == batch_id)
                .values(status=SyncTargetStatus.PUBLISHED)
            )

            batch.status = SyncBatchStatus.PUBLISHED
            batch.published_publication_id = publication_id
            batch.finished_at = self._now()
            batch.rows_received = rows_received
            batch.rows_new = counts[BarObservationDisposition.NEW]
            batch.rows_changed = (
                counts[BarObservationDisposition.CHANGED] + counts[BarObservationDisposition.REVERTED]
            )
            batch.rows_unchanged = counts[BarObservationDisposition.UNCHANGED]

            run.pages_received += pages_received
            _reaggregate_run(session, run)
            session.commit()

    # ------------------------------------------------------- master + snapshot

    def _complete(self, run_id: uuid.UUID, plan: _FrozenPlan) -> SyncOutcome:
        with self._sessions() as session:
            run = session.get(SyncRun, run_id)
            observed = _observed_row_count(session, run_id)
            mode = run.mode

        if observed == 0:
            return self._complete_without_bars(run_id, plan, mode)

        self._check_cancelled(run_id)
        master_snapshot_id = self._ensure_master(run_id, plan)
        evaluation = self._evaluate_quality(run_id, plan)
        return self._activate_snapshot(run_id, plan, master_snapshot_id, evaluation)

    def _evaluate_quality(self, run_id: uuid.UUID, plan: _FrozenPlan) -> _Evaluation:
        """Run the contextual rules and report whether the snapshot is usable.

        A fresh evaluation row per attempt, rather than one per run: a run that
        reaches this phase, writes its findings and then fails at activation is
        resumable, and re-running the pass under the same anchor would collide
        with the findings the first attempt already wrote.

        The failure path records that the pass produced no verdict and re-raises
        rather than deciding anything. A crashed evaluator said neither "the
        data is fine" nor "the data is bad", and writing ineligible would be
        indistinguishable from having checked. Letting the Task fail keeps the
        two apart, and leaves the previous snapshot as head.
        """
        with self._sessions() as session:
            run = session.get(SyncRun, run_id)
            run.phase = SyncPhase.EVALUATING_QUALITY
            evaluation = QualityEvaluation(
                kind=QualityEvaluationKind.SYNC,
                source=self._source,
                sync_run_id=run_id,
                status=QualityEvaluationStatus.RUNNING,
                policy=policy_snapshot(self._quality_policy),
            )
            session.add(evaluation)
            session.flush()
            evaluation_id = evaluation.id
            session.commit()

        calendar = DbCalendarPort(self._sessions, plan.calendar_publication_id)
        try:
            with self._sessions() as session:
                outcome = quality_pass.evaluate(
                    session,
                    evaluation_id=evaluation_id,
                    scope=quality_pass.RunScope(run_id),
                    source=self._source,
                    calendar=calendar,
                    calendar_publication_id=plan.calendar_publication_id,
                    policy=self._quality_policy,
                )
                session.commit()
        except Exception as exc:
            with self._sessions() as session:
                failed = session.get(QualityEvaluation, evaluation_id)
                failed.status = QualityEvaluationStatus.FAILED
                failed.error_summary = str(exc)
                session.commit()
            raise

        with self._sessions() as session:
            done = session.get(QualityEvaluation, evaluation_id)
            done.status = QualityEvaluationStatus.SUCCEEDED
            session.commit()
        return _Evaluation(id=evaluation_id, is_backtest_eligible=outcome.is_backtest_eligible)

    def _complete_without_bars(
        self, run_id: uuid.UUID, plan: _FrozenPlan, mode: SyncMode | None
    ) -> SyncOutcome:
        """No rows observed anywhere in the run.

        INITIAL and FULL_RECONCILE must fail here: an empty result cannot be
        distinguished from a broken source, and letting it through would
        advance the full-reconcile watermark on no evidence.
        """
        if mode == SyncMode.INITIAL:
            raise _CodedError("empty_initial_bars", "Initial sync observed no daily bars")
        if mode == SyncMode.FULL_RECONCILE:
            raise _CodedError(
                "empty_full_reconcile_plan", "Full reconcile observed no daily bars"
            )
        return self._inherit_snapshot(run_id, plan)

    def _ensure_master(self, run_id: uuid.UUID, plan: _FrozenPlan) -> uuid.UUID:
        """Fill in every weekly roster the calendar calls for, and return the
        one dated at `coverage_end`.

        A pool built for a past date needs the roster *of that date*: which
        securities were listed, on which market tier, as what kind of security.
        Fetching only today's roster and reusing it for history drops every
        security delisted since (measured against the real feed: 88 gone and 28
        demoted out of 1643 over two years) while smuggling in 35 that were not
        yet Prime — the second half being look-ahead, not merely survivorship.

        Weekly, because the decision day is weekly: a finer grain buys accuracy
        only for mid-week delistings, at 4.7x the requests.
        """
        with self._sessions() as session:
            run = session.get(SyncRun, run_id)
            run.phase = SyncPhase.MASTER
            session.commit()
            coverage = _run_coverage_end(session, run_id)

        if coverage is None:
            raise SyncInvariantError("Bars were observed but no coverage date could be derived")

        with self._sessions() as session:
            targets = _roster_targets(session, plan.calendar_publication_id, coverage)
            # What is already stored *is* the progress record. No checklist
            # table: it could only ever be a shadow of these rows, and one that
            # can disagree with them. An interrupted backfill resumes by
            # recomputing this difference.
            missing = sorted(targets - _stored_roster_dates(session, self._source))

        for as_of in missing:
            self._check_cancelled(run_id)
            self._fetch_roster(run_id, as_of)

        with self._sessions() as session:
            snapshot_id = _roster_id_for(session, self._source, coverage)
        if snapshot_id is None:
            raise SyncInvariantError(f"No published roster exists for coverage end {coverage}")
        return snapshot_id

    def _fetch_roster(self, run_id: uuid.UUID, as_of: date) -> uuid.UUID:
        publication_id = self._begin_publication(
            run_id,
            MASTER_ENDPOINT,
            {"date": as_of.isoformat()},
            # The roster's own date identifies the scope, so `attempt` keeps
            # meaning "retry of this roster" rather than "the Nth roster this
            # run fetched" — which is what a fixed ordinal would have turned it
            # into once a run fetches a hundred of them.
            scope_ordinal=as_of.toordinal(),
        )
        try:
            result = self._fetch(lambda: self._adapter.fetch_master(as_of.isoformat()), "master")
            if not result.rows:
                raise ValueError("J-Quants master response was empty")
            self._store_pages(publication_id, result.pages)
            snapshot_id = self._ingest_master(run_id, publication_id, as_of, result.rows)
        except Exception as exc:
            self._abandon_publication(publication_id, None, _error_code(exc), str(exc))
            raise

        with self._sessions() as session:
            run, task = _lock_run_and_task(session, run_id)
            _require_active(run, task)
            publication = session.get(EndpointPublication, publication_id, with_for_update=True)
            publication.status = PublicationStatus.PUBLISHED
            publication.publish_sequence = _next_publish_sequence(session)
            publication.published_at = self._now()
            publication.schema_fingerprint = _schema_fingerprint(result.rows)
            publication.stats = {"pages": result.page_count, "rows": len(result.rows)}
            session.commit()
        return snapshot_id

    def _ingest_master(
        self,
        run_id: uuid.UUID,
        publication_id: uuid.UUID,
        as_of_date: date,
        rows: list[dict[str, Any]],
    ) -> uuid.UUID:
        with self._sessions() as session:
            snapshot = InstrumentMasterSnapshot(
                source=self._source,
                as_of_date=as_of_date,
                sync_run_id=run_id,
                publication_id=publication_id,
            )
            session.add(snapshot)
            session.flush()
            for row in rows:
                code = str(_required(row, "Code"))
                market = str(_required(row, "Mkt"))
                instrument = _instrument(session, self._source, code)
                security_class = "common_stock_inferred" if code.endswith("0") else "other_class_inferred"
                instrument.classification = security_class
                values = {
                    "symbol": code,
                    "company_name": row.get("CoName"),
                    "company_name_en": row.get("CoNameEn"),
                    "market_code": market,
                    "market_name": row.get("MktNm"),
                    "sector_17": row.get("S17"),
                    "sector_33": row.get("S33"),
                    "scale_category": row.get("ScaleCat"),
                    "product_category": row.get("ProdCat"),
                    "inferred_security_class": security_class,
                    "classification_method": "jpx_code_suffix_v1",
                }
                session.add(
                    InstrumentMasterSnapshotMember(
                        snapshot_id=snapshot.id,
                        instrument_id=instrument.instrument_id,
                        content_hash=_hash(values),
                        **values,
                    )
                )
            session.commit()
            return snapshot.id

    def _activate_snapshot(
        self,
        run_id: uuid.UUID,
        plan: _FrozenPlan,
        master_snapshot_id: uuid.UUID,
        evaluation: _Evaluation,
    ) -> SyncOutcome:
        """Create the immutable snapshot, switch the head, and terminate both
        SyncRun and Task — all in one transaction (§9.2)."""
        with self._sessions() as session:
            run, task = _lock_run_and_task(session, run_id)
            head = session.get(DataSnapshotHead, self._source, with_for_update=True)
            _require_active(run, task)
            run.phase = SyncPhase.ACTIVATING_SNAPSHOT

            unfinished = session.scalar(
                select(func.count())
                .select_from(SyncBatch)
                .where(SyncBatch.sync_run_id == run_id, SyncBatch.status != SyncBatchStatus.PUBLISHED)
            )
            if unfinished:
                raise SyncInvariantError("Cannot activate a snapshot while batches remain unpublished")

            cutoff = _run_bars_cutoff(session, run_id)
            if cutoff is None:
                raise SyncInvariantError("No published bars publication to anchor the snapshot")

            coverage = snapshot_reader.resolve_coverage(session, self._source, cutoff)
            if coverage.start is None or coverage.end is None:
                raise SyncInvariantError("Snapshot cutoff resolves to no readable bars")
            outside = snapshot_reader.count_members_outside(
                session,
                self._source,
                cutoff,
                coverage_start=coverage.start,
                coverage_end=coverage.end,
            )
            if outside:
                raise SyncInvariantError("Resolved snapshot members fall outside the declared coverage")

            snapshot = DataSnapshot(
                source=self._source,
                sync_run_id=run_id,
                mode=run.mode,
                bar_publish_sequence=cutoff,
                master_publish_sequence=_master_cutoff(session),
                calendar_publication_id=plan.calendar_publication_id,
                master_snapshot_id=master_snapshot_id,
                coverage_start=coverage.start,
                coverage_end=coverage.end,
                verified_start=plan.target_dates[0] if plan.target_dates else None,
                verified_end=plan.target_dates[-1] if plan.target_dates else None,
                plan_fingerprint=plan.fingerprint,
                is_backtest_eligible=evaluation.is_backtest_eligible,
                version=source_state.next_snapshot_version(session, self._source),
            )
            session.add(snapshot)
            session.flush()
            source_state.set_head(session, head, self._source, snapshot.id, self._now())
            # Closed here, in the activation transaction, because until now the
            # evaluation had judged data no snapshot existed for.
            session.get(QualityEvaluation, evaluation.id).produced_snapshot_id = snapshot.id

            terminal = (
                SyncRunStatus.SUCCEEDED
                if (run.rows_new + run.rows_changed) > 0
                else SyncRunStatus.NO_CHANGE
            )
            run.phase = SyncPhase.COMPLETE
            run.current_batch = None
            _terminate(run, task, terminal, self._now())
            session.commit()

            return SyncOutcome(
                run_id=run_id,
                status=terminal,
                mode=snapshot.mode,
                snapshot_id=snapshot.id,
                coverage_start=snapshot.coverage_start,
                coverage_end=snapshot.coverage_end,
            )

    def _inherit_snapshot(self, run_id: uuid.UUID, plan: _FrozenPlan) -> SyncOutcome:
        """Empty INCREMENTAL: create a semantically equivalent new snapshot that
        inherits the current head's cutoff, coverage and master (§12)."""
        with self._sessions() as session:
            run, task = _lock_run_and_task(session, run_id)
            head = session.get(DataSnapshotHead, self._source, with_for_update=True)
            _require_active(run, task)
            if head is None:
                raise SyncInvariantError("Empty incremental sync has no snapshot head to inherit from")
            previous = session.get(DataSnapshot, head.snapshot_id)

            snapshot = DataSnapshot(
                source=self._source,
                sync_run_id=run_id,
                mode=run.mode,
                bar_publish_sequence=previous.bar_publish_sequence,
                # Nothing was fetched, so the roster population is unchanged
                # too; inheriting keeps the two snapshots resolving identically.
                master_publish_sequence=previous.master_publish_sequence,
                calendar_publication_id=plan.calendar_publication_id,
                master_snapshot_id=previous.master_snapshot_id,
                coverage_start=previous.coverage_start,
                coverage_end=previous.coverage_end,
                verified_start=plan.target_dates[0] if plan.target_dates else None,
                verified_end=plan.target_dates[-1] if plan.target_dates else None,
                plan_fingerprint=plan.fingerprint,
                # Nothing new was published, so this snapshot resolves to
                # byte-identical bar versions. Re-running the pass would spend
                # time proving an answer that cannot have changed.
                is_backtest_eligible=previous.is_backtest_eligible,
                version=source_state.next_snapshot_version(session, self._source),
            )
            session.add(snapshot)
            session.flush()
            source_state.set_head(session, head, self._source, snapshot.id, self._now())

            run.phase = SyncPhase.COMPLETE
            run.current_batch = None
            _terminate(run, task, SyncRunStatus.NO_CHANGE, self._now())
            session.commit()

            return SyncOutcome(
                run_id=run_id,
                status=SyncRunStatus.NO_CHANGE,
                mode=snapshot.mode,
                snapshot_id=snapshot.id,
                coverage_start=snapshot.coverage_start,
                coverage_end=snapshot.coverage_end,
            )

    # -------------------------------------------------------------- terminating

    def _fail(self, run_id: uuid.UUID, exc: Exception) -> None:
        with self._sessions() as session:
            run, task = _lock_run_and_task(session, run_id)
            if run.status in TERMINAL_RUN_STATUSES:
                return
            published = session.scalar(
                select(func.count())
                .select_from(EndpointPublication)
                .where(
                    EndpointPublication.sync_run_id == run_id,
                    EndpointPublication.status == PublicationStatus.PUBLISHED,
                )
            )
            status = SyncRunStatus.PARTIAL_FAILED if published else SyncRunStatus.FAILED
            run.error_code = _error_code(exc)
            run.error_summary = str(exc)
            _terminate(run, task, status, self._now())
            session.commit()

    def _confirm_cancellation(self, run_id: uuid.UUID) -> None:
        """Reached a safe point: unwind staging state, then terminate both."""
        with self._sessions() as session:
            run, task = _lock_run_and_task(session, run_id)
            if run.status in TERMINAL_RUN_STATUSES:
                return
            session.execute(
                EndpointPublication.__table__.update()
                .where(
                    EndpointPublication.sync_run_id == run_id,
                    EndpointPublication.status == PublicationStatus.STAGING,
                )
                .values(status=PublicationStatus.CANCELLED, error_code="cancelled")
            )
            session.execute(
                SyncBatch.__table__.update()
                .where(SyncBatch.sync_run_id == run_id, SyncBatch.status != SyncBatchStatus.PUBLISHED)
                .values(status=SyncBatchStatus.CANCELLED)
            )
            session.execute(
                SyncTargetDate.__table__.update()
                .where(
                    SyncTargetDate.sync_run_id == run_id,
                    SyncTargetDate.status != SyncTargetStatus.PUBLISHED,
                )
                .values(status=SyncTargetStatus.CANCELLED)
            )
            run.current_batch = None
            _terminate(run, task, SyncRunStatus.CANCELLED, self._now())
            session.commit()

    def _check_cancelled(self, run_id: uuid.UUID) -> None:
        with self._sessions() as session:
            run = session.get(SyncRun, run_id)
            if run.cancel_requested_at is not None or run.status == SyncRunStatus.CANCELLING:
                raise SyncCancelled("Cancellation requested")

    # ---------------------------------------------------------------- plumbing

    def _fetch(self, call: Callable[[], FetchResult], what: str) -> FetchResult:
        try:
            return call()
        except JQuantsError as exc:
            raise _CodedError("source_unavailable", f"J-Quants {what} request failed: {exc}") from exc

    def _begin_publication(
        self,
        run_id: uuid.UUID,
        endpoint: str,
        params: dict[str, Any],
        *,
        scope_ordinal: int,
        batch_id: uuid.UUID | None = None,
        attempt: int | None = None,
    ) -> uuid.UUID:
        with self._sessions() as session:
            run = session.get(SyncRun, run_id)
            task = session.get(Task, run.task_id)
            if attempt is None:
                highest = session.scalar(
                    select(func.max(EndpointPublication.attempt)).where(
                        EndpointPublication.sync_run_id == run_id,
                        EndpointPublication.endpoint == endpoint,
                        EndpointPublication.scope_ordinal == scope_ordinal,
                    )
                )
                attempt = (highest or 0) + 1
            publication = EndpointPublication(
                sync_run_id=run_id,
                sync_batch_id=batch_id,
                created_by_task_id=run.task_id,
                created_by_task_attempt=task.attempt_count,
                endpoint=endpoint,
                scope_ordinal=scope_ordinal,
                attempt=attempt,
                status=PublicationStatus.STAGING,
                api_version=self._adapter.API_VERSION,
                adapter_version=self._adapter.ADAPTER_VERSION,
                request_params=params,
            )
            session.add(publication)
            session.commit()
            return publication.id

    def _store_pages(
        self, publication_id: uuid.UUID, pages: list[dict[str, Any]], start_index: int = 0
    ) -> None:
        expires = self._now() + RAW_PAGE_RETENTION
        with self._sessions() as session:
            for offset, payload in enumerate(pages):
                session.add(
                    RawSourcePage(
                        publication_id=publication_id,
                        page_index=start_index + offset,
                        payload=payload,
                        content_hash=_hash(payload),
                        expires_at=expires,
                    )
                )
            session.commit()

    def _abandon_publication(
        self,
        publication_id: uuid.UUID,
        batch_id: uuid.UUID | None,
        code: str,
        summary: str,
        status: PublicationStatus = PublicationStatus.FAILED,
    ) -> None:
        """Observations and raw pages survive as audit fact; only the
        publication loses its claim to being official."""
        with self._sessions() as session:
            publication = session.get(EndpointPublication, publication_id, with_for_update=True)
            if publication.status == PublicationStatus.STAGING:
                publication.status = status
                publication.error_code = code
                publication.error_summary = summary
            if batch_id is not None:
                batch = session.get(SyncBatch, batch_id, with_for_update=True)
                if batch.status != SyncBatchStatus.PUBLISHED:
                    batch.status = (
                        SyncBatchStatus.CANCELLED
                        if status == PublicationStatus.CANCELLED
                        else SyncBatchStatus.FAILED
                    )
                    batch.error_code = code
                    batch.error_summary = summary
                    batch.finished_at = self._now()
            session.commit()

    def _view(self, session: Session, run: SyncRun, *, include_batches: bool = False) -> SyncRunView:
        task = session.get(Task, run.task_id)
        snapshot = session.scalar(select(DataSnapshot).where(DataSnapshot.sync_run_id == run.id))
        batches: list[dict[str, Any]] = []
        if include_batches:
            rows = session.scalars(
                select(SyncBatch).where(SyncBatch.sync_run_id == run.id).order_by(SyncBatch.ordinal)
            ).all()
            batches = [
                {
                    "ordinal": item.ordinal,
                    "status": item.status.value,
                    "target_start": item.target_start,
                    "target_end": item.target_end,
                    "target_dates": item.target_dates,
                    "attempt_count": item.attempt_count,
                    "rows_received": item.rows_received,
                    "error_code": item.error_code,
                }
                for item in rows
            ]
        return SyncRunView(
            id=run.id,
            task_id=run.task_id,
            source=run.source,
            status=run.status,
            phase=run.phase,
            mode=run.mode,
            task_attempt=task.attempt_count if task else 0,
            target_dates=run.target_dates,
            processed_dates=run.processed_dates,
            total_batches=run.total_batches,
            completed_batches=run.completed_batches,
            current_batch=run.current_batch,
            coverage_before=run.coverage_before,
            planned_start=run.planned_start,
            planned_end=run.planned_end,
            rows_received=run.rows_received,
            rows_new=run.rows_new,
            rows_changed=run.rows_changed,
            rows_unchanged=run.rows_unchanged,
            pages_received=run.pages_received,
            actual_min=run.actual_min,
            actual_max=run.actual_max,
            snapshot_id=snapshot.id if snapshot else None,
            resumable=is_resumable(session, run, task, self._policy),
            current_date=_current_target_date(session, run.id),
            error_code=run.error_code,
            error_summary=run.error_summary,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            batches=batches,
        )


@dataclass(frozen=True)
class _FrozenPlan:
    mode: SyncMode
    fingerprint: str
    target_dates: list[date]
    calendar_publication_id: uuid.UUID


@dataclass(frozen=True)
class _Evaluation:
    """What the quality phase hands to snapshot activation: the verdict, and
    the row to attach the resulting snapshot to."""

    id: uuid.UUID
    is_backtest_eligible: bool


class _CodedError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------- helpers


def _lock_run_and_task(session: Session, run_id: uuid.UUID) -> tuple[SyncRun, Task]:
    """Always SyncRun then Task. Publish and cancel transactions share this
    order, which is what makes their race resolvable by commit order."""
    run = session.get(SyncRun, run_id, with_for_update=True)
    if run is None:
        raise LookupError("Sync run not found")
    task = session.get(Task, run.task_id, with_for_update=True)
    return run, task


def _require_active(run: SyncRun, task: Task) -> None:
    if run.status != SyncRunStatus.RUNNING or task.status != TaskStatus.RUNNING:
        raise SyncCancelled("Sync run is no longer running")
    if run.cancel_requested_at is not None:
        raise SyncCancelled("Cancellation was requested")


def _terminate(run: SyncRun, task: Task, status: SyncRunStatus, when: datetime) -> None:
    """The only way a run reaches a terminal state: both rows, one transaction."""
    run.status = status
    run.finished_at = when
    task.status = _TASK_TERMINAL[status]
    task.finished_at = when


_TASK_TERMINAL = {
    SyncRunStatus.SUCCEEDED: TaskStatus.SUCCEEDED,
    SyncRunStatus.NO_CHANGE: TaskStatus.SUCCEEDED,
    SyncRunStatus.FAILED: TaskStatus.FAILED,
    SyncRunStatus.PARTIAL_FAILED: TaskStatus.FAILED,
    SyncRunStatus.CANCELLED: TaskStatus.CANCELLED,
}


def is_resumable(
    session: Session, run: SyncRun, task: Task | None, policy: SyncPolicy = SyncPolicy()
) -> bool:
    """Derived, never stored — a persisted boolean would drift from the
    checkpoint it claims to describe (§6.1)."""
    if run.status in {SyncRunStatus.SUCCEEDED, SyncRunStatus.NO_CHANGE, SyncRunStatus.QUEUED}:
        return False
    if run.error_code in NON_RESUMABLE_ERROR_CODES:
        return False
    if task is not None and task.attempt_count >= policy.max_task_attempts:
        return False
    return True


def _reset_unpublished_batches(session: Session, run: SyncRun) -> None:
    session.execute(
        SyncBatch.__table__.update()
        .where(
            SyncBatch.sync_run_id == run.id,
            SyncBatch.status.in_([SyncBatchStatus.FAILED, SyncBatchStatus.CANCELLED, SyncBatchStatus.STAGING]),
        )
        .values(status=SyncBatchStatus.PENDING, error_code=None, error_summary=None, finished_at=None)
    )
    session.execute(
        SyncTargetDate.__table__.update()
        .where(
            SyncTargetDate.sync_run_id == run.id,
            SyncTargetDate.status != SyncTargetStatus.PUBLISHED,
        )
        .values(status=SyncTargetStatus.PENDING)
    )


def _calendar_publication(
    session: Session, run_id: uuid.UUID, status: PublicationStatus
) -> EndpointPublication | None:
    return session.scalars(
        select(EndpointPublication)
        .where(
            EndpointPublication.sync_run_id == run_id,
            EndpointPublication.endpoint == CALENDAR_ENDPOINT,
            EndpointPublication.status == status,
        )
        .order_by(EndpointPublication.attempt.desc())
        .limit(1)
    ).first()


def _load_frozen_plan(session: Session, run: SyncRun) -> _FrozenPlan:
    dates = session.scalars(
        select(SyncTargetDate.trade_date)
        .where(SyncTargetDate.sync_run_id == run.id)
        .order_by(SyncTargetDate.trade_date)
    ).all()
    calendar = _calendar_publication(session, run.id, PublicationStatus.PUBLISHED)
    return _FrozenPlan(
        mode=run.mode,
        fingerprint=run.plan_fingerprint,
        target_dates=list(dates),
        calendar_publication_id=calendar.id,
    )


def _snapshot_watermarks(session: Session, source: str) -> tuple[date | None, datetime | None]:
    """Coverage comes from the snapshot head, and the full-reconcile watermark
    only from runs that actually created a snapshot — PARTIAL_FAILED must not
    advance it (§10)."""
    head = session.get(DataSnapshotHead, source)
    if head is None:
        return None, None
    current = session.get(DataSnapshot, head.snapshot_id)
    last_full = session.scalars(
        select(DataSnapshot)
        .where(
            DataSnapshot.source == source,
            DataSnapshot.mode.in_([SyncMode.INITIAL, SyncMode.FULL_RECONCILE]),
        )
        .order_by(DataSnapshot.created_at.desc())
        .limit(1)
    ).first()
    return current.coverage_end, (last_full.created_at if last_full else None)


def _next_publish_sequence(session: Session) -> int:
    return session.scalar(select(publish_sequence_seq.next_value()))


def _current_target_date(session: Session, run_id: uuid.UUID) -> date | None:
    """The date being fetched right now — staging progress, shown separately
    from the published totals and never folded into them (§16)."""
    return session.scalar(
        select(func.min(SyncTargetDate.trade_date)).where(
            SyncTargetDate.sync_run_id == run_id,
            SyncTargetDate.status == SyncTargetStatus.STAGING,
        )
    )


def _observed_row_count(session: Session, run_id: uuid.UUID) -> int:
    return session.scalar(
        select(func.count())
        .select_from(PublicationBarObservation)
        .join(
            EndpointPublication,
            EndpointPublication.id == PublicationBarObservation.publication_id,
        )
        .where(
            EndpointPublication.sync_run_id == run_id,
            EndpointPublication.endpoint == BARS_ENDPOINT,
            EndpointPublication.status == PublicationStatus.PUBLISHED,
        )
    )


def _run_bars_cutoff(session: Session, run_id: uuid.UUID) -> int | None:
    return session.scalar(
        select(func.max(EndpointPublication.publish_sequence)).where(
            EndpointPublication.sync_run_id == run_id,
            EndpointPublication.endpoint == BARS_ENDPOINT,
            EndpointPublication.status == PublicationStatus.PUBLISHED,
        )
    )


def _master_cutoff(session: Session) -> int | None:
    """Highest published roster sequence at activation time.

    Everything this run fetched is at or below it; anything a *later* sync
    publishes is above, which is what keeps a finished snapshot from acquiring
    new roster history after the fact.
    """
    return session.scalar(
        select(func.max(EndpointPublication.publish_sequence)).where(
            EndpointPublication.endpoint == MASTER_ENDPOINT,
            EndpointPublication.status == PublicationStatus.PUBLISHED,
        )
    )


def _roster_targets(
    session: Session, calendar_publication_id: uuid.UUID, coverage_end: date
) -> set[date]:
    """Which dates the instrument roster should exist for: one per ISO week —
    that week's last open trading day — plus `coverage_end` itself.

    The week's *last* open day is deliberate: it is the decision day the pool
    will be asked about, so the roster lands exactly where it is needed instead
    of one to four days stale. `coverage_end` is added because the snapshot's
    own `master_snapshot_id` points at it, and it need not be a week's end.
    """
    days = session.scalars(
        select(TradingCalendar.trade_date)
        .where(
            TradingCalendar.publication_id == calendar_publication_id,
            TradingCalendar.market == MARKET_TSE,
            TradingCalendar.is_open.is_(True),
            TradingCalendar.trade_date <= coverage_end,
        )
        .order_by(TradingCalendar.trade_date)
    ).all()
    weekly: dict[tuple[int, int], date] = {}
    for day in days:
        weekly[day.isocalendar()[:2]] = day
    return set(weekly.values()) | {coverage_end}


def _stored_roster_dates(session: Session, source: str) -> set[date]:
    """Roster dates already held *usefully*.

    Two exclusions, both there so the backfill heals rather than entrenches:

    - unpublished ones, so an abandoned attempt does not make its date look
      done and get skipped forever;
    - ones whose members carry no product category, which is what every roster
      ingested before that column existed looks like. Such a roster cannot
      answer the identity question at all, and counting it as present would
      leave a pool built on it silently empty — the failure mode with no error
      message attached.
    """
    incomplete = exists().where(
        InstrumentMasterSnapshotMember.snapshot_id == InstrumentMasterSnapshot.id,
        InstrumentMasterSnapshotMember.product_category.is_(None),
    )
    return set(
        session.scalars(
            select(InstrumentMasterSnapshot.as_of_date)
            .join(
                EndpointPublication,
                EndpointPublication.id == InstrumentMasterSnapshot.publication_id,
            )
            .where(
                InstrumentMasterSnapshot.source == source,
                EndpointPublication.status == PublicationStatus.PUBLISHED,
                ~incomplete,
            )
        ).all()
    )


def _roster_id_for(session: Session, source: str, as_of: date) -> uuid.UUID | None:
    return session.scalar(
        select(InstrumentMasterSnapshot.id)
        .join(
            EndpointPublication,
            EndpointPublication.id == InstrumentMasterSnapshot.publication_id,
        )
        .where(
            InstrumentMasterSnapshot.source == source,
            InstrumentMasterSnapshot.as_of_date == as_of,
            EndpointPublication.status == PublicationStatus.PUBLISHED,
        )
        .order_by(EndpointPublication.publish_sequence.desc())
        .limit(1)
    )


def _run_coverage_end(session: Session, run_id: uuid.UUID) -> date | None:
    """Latest trade date this run actually observed — the master's as-of date."""
    return session.scalar(
        select(func.max(BarRecord.trade_date))
        .select_from(PublicationBarObservation)
        .join(BarRecord, BarRecord.id == PublicationBarObservation.bar_record_id)
        .join(
            EndpointPublication,
            EndpointPublication.id == PublicationBarObservation.publication_id,
        )
        .where(
            EndpointPublication.sync_run_id == run_id,
            EndpointPublication.endpoint == BARS_ENDPOINT,
            EndpointPublication.status == PublicationStatus.PUBLISHED,
        )
    )


def _reaggregate_run(session: Session, run: SyncRun) -> None:
    """Recompute from published batches rather than `+=`, so a retried batch
    can never double-count (§11)."""
    totals = session.execute(
        select(
            func.count(),
            func.coalesce(func.sum(SyncBatch.target_dates), 0),
            func.coalesce(func.sum(SyncBatch.rows_received), 0),
            func.coalesce(func.sum(SyncBatch.rows_new), 0),
            func.coalesce(func.sum(SyncBatch.rows_changed), 0),
            func.coalesce(func.sum(SyncBatch.rows_unchanged), 0),
        ).where(SyncBatch.sync_run_id == run.id, SyncBatch.status == SyncBatchStatus.PUBLISHED)
    ).one()
    run.completed_batches = totals[0]
    run.processed_dates = totals[1]
    run.rows_received = totals[2]
    run.rows_new = totals[3]
    run.rows_changed = totals[4]
    run.rows_unchanged = totals[5]

    bounds = session.execute(
        select(func.min(BarRecord.trade_date), func.max(BarRecord.trade_date))
        .select_from(PublicationBarObservation)
        .join(BarRecord, BarRecord.id == PublicationBarObservation.bar_record_id)
        .join(
            EndpointPublication,
            EndpointPublication.id == PublicationBarObservation.publication_id,
        )
        .where(
            EndpointPublication.sync_run_id == run.id,
            EndpointPublication.endpoint == BARS_ENDPOINT,
            EndpointPublication.status == PublicationStatus.PUBLISHED,
        )
    ).one()
    run.actual_min, run.actual_max = bounds[0], bounds[1]


def _disposition_counts(
    observations: list[PublicationBarObservation],
) -> dict[BarObservationDisposition, int]:
    counts = {item: 0 for item in BarObservationDisposition}
    for observation in observations:
        counts[observation.disposition] += 1
    return counts


def _instrument(session: Session, source: str, code: str) -> Instrument:
    instrument = session.scalar(
        select(Instrument).where(Instrument.source == source, Instrument.source_code == code)
    )
    if instrument is None:
        instrument = Instrument(source=source, source_code=code, exchange="TSE", currency="JPY")
        session.add(instrument)
        session.flush()
    return instrument


def _bar_record(
    session: Session, source: str, instrument_id: uuid.UUID, trade_date: date, session_name: str = "full_day"
) -> BarRecord:
    record = session.scalar(
        select(BarRecord).where(
            BarRecord.source == source,
            BarRecord.instrument_id == instrument_id,
            BarRecord.trade_date == trade_date,
            BarRecord.session == session_name,
        )
    )
    if record is None:
        record = BarRecord(
            source=source, instrument_id=instrument_id, trade_date=trade_date, session=session_name
        )
        session.add(record)
        session.flush()
    return record


def _calendar_dates(rows: list[dict[str, Any]]) -> list[date]:
    """The dates this run should fetch: every day the market was open.

    Half-day sessions (HolDiv=2) count — they trade, and skipping them is what
    left a hole in the data 03 shipped.
    """
    return [day.trade_date for day in normalize_calendar(rows) if day.is_open]


def _bar_values(row: dict[str, Any]) -> dict[str, Decimal | None]:
    mapping = {
        "raw_open": "O", "raw_high": "H", "raw_low": "L", "raw_close": "C",
        "raw_volume": "Vo", "trading_value": "Va", "adjusted_open": "AdjO",
        "adjusted_high": "AdjH", "adjusted_low": "AdjL", "adjusted_close": "AdjC",
        "adjusted_volume": "AdjVo", "adjustment_factor": "AdjFactor",
    }
    return {target: _decimal(row.get(source)) for target, source in mapping.items()}


def _required(row: dict[str, Any], field_name: str) -> Any:
    value = row.get(field_name)
    if value is None or value == "":
        raise ValueError(f"J-Quants row is missing required field {field_name}")
    return value


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("J-Quants numeric field has an invalid type") from exc


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _schema_fingerprint(rows: list[dict[str, Any]]) -> str | None:
    if not rows:
        return None
    return _hash(sorted({key for row in rows for key in row}))


def _error_code(exc: Exception) -> str:
    if isinstance(exc, _CodedError):
        return exc.code
    if isinstance(exc, SyncInvariantError):
        return "sync_invariant"
    if isinstance(exc, JQuantsError):
        return "source_unavailable"
    return "sync_failed"
