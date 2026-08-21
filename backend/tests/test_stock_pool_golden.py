"""Golden test: one real decision day, pinned.

The constructed tests next door prove each rule fires. They cannot prove the
rules add up to the right answer on the real market, because the data they run
on was written by the same understanding as the code. Ticket 04 was bitten by
exactly that twice — the adjusted-price rounding and the all-null no-trade rows
were both invisible until production data arrived.

So this runs on a slice of the real feed, exported once from a real sync
(`.master-backfill/export_golden.py`) and committed. No network: ticket 03
requires ordinary CI to stay off J-Quants.

The numbers below were checked before being pinned, not copied out of the
code's own output:

* 1,562 Prime domestic common stocks on 2026-05-22 — the same figure an
  independent probe script counted straight off the API response, through a
  different code path.
* 810 of them clear the 500M yen floor on a 20-day average. A single-day cut of
  the same data passes 755 (48.3%); averaging lifts it, since a security below
  the line on one day can be above it across twenty. Both sit either side of
  the median Prime turnover of 462.6M, which is where a 500M floor has to land.
"""

from __future__ import annotations

import gzip
import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

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
)
from app.models.task import Task
from app.services.calendar_normalization import FULL_DAY, CalendarDay
from app.services.stock_pool import PoolExclusionReason, build_stock_pool
from tests.fakes import InMemoryCalendarPort

GOLDEN = Path(__file__).parent / "data" / "stock_pool_golden.json.gz"

EXPECTED_PRIME_COMMON = 1562
EXPECTED_POOL_SIZE = 810


@pytest.fixture(scope="module")
def golden() -> dict:
    with gzip.open(GOLDEN, "rt", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def loaded(golden, engine):
    """Rebuild the slice as ordinary rows.

    Written straight in rather than replayed through the sync workflow: the
    fixture holds 23 days out of 487, which no plan the workflow would draw up
    corresponds to. What matters is that the pool reads the same shapes it
    reads in production, and it does.
    """
    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
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

    instruments = {
        row["symbol"]: uuid.uuid4() for row in golden["roster"]
    }
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
                "adjusted_close": _decimal(row["adjusted_close"]),
                "raw_volume": _decimal(row["volume"]),
                "trading_value": _decimal(row["turnover"]),
                "quality_status": BarQualityStatus(row["quality_status"]),
                "quality_rules": [],
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
        plan_fingerprint="golden",
    )
    session.add(snapshot)
    session.commit()

    calendar = InMemoryCalendarPort(
        [
            CalendarDay(
                trade_date=date.fromisoformat(day),
                hol_div="1",
                is_open=True,
                session=FULL_DAY,
            )
            for day in golden["calendar"]
        ]
    )
    yield session, snapshot, calendar
    session.close()
    outer.rollback()
    connection.close()


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


@pytest.fixture(scope="module")
def pool(golden, loaded):
    session, snapshot, calendar = loaded
    return build_stock_pool(
        session,
        snapshot,
        as_of=date.fromisoformat(golden["as_of"]),
        calendar=calendar,
    )


def test_the_investable_universe_is_the_size_the_source_says_it_is(pool):
    """Members plus exclusions is everything that passed the three identity
    layers — the figure an independent count off the raw API agreed on."""
    assert len(pool.members) + len(pool.exclusions) == EXPECTED_PRIME_COMMON


def test_the_pool_is_pinned(pool):
    assert len(pool.members) == EXPECTED_POOL_SIZE


def test_the_floor_is_what_removes_almost_everyone(pool):
    """Liquidity is the binding constraint, not data gaps. If this ever flips,
    something is wrong with the data rather than with the market."""
    from collections import Counter

    reasons = Counter(reason for item in pool.exclusions for reason in item.reasons)
    assert reasons[PoolExclusionReason.AVERAGE_TURNOVER_BELOW_FLOOR] == 747
    assert reasons[PoolExclusionReason.INSUFFICIENT_PRICE_HISTORY] == 8
    assert reasons[PoolExclusionReason.MISSING_BARS_IN_LIQUIDITY_WINDOW] == 0


def test_the_largest_japanese_companies_are_in_it(pool):
    """A pool that dropped Toyota or Sony would be broken in a way no count
    would reveal."""
    symbols = {member.symbol for member in pool.members}

    assert {"72030", "67580", "83060", "99840"} <= symbols


def test_non_common_stock_never_reaches_the_result(pool):
    """Real codes, each excluded by a different layer: an ETF and a REIT by
    market tier, a foreign issuer by product category, preferred shares by
    suffix. None may appear as a member *or* as an exclusion."""
    seen = {item.symbol for item in pool.members} | {item.symbol for item in pool.exclusions}

    for symbol in ("13050", "89510", "17730", "25935", "94345"):
        assert symbol not in seen


def test_every_member_clears_the_floor(pool):
    assert min(member.average_turnover for member in pool.members) >= Decimal("500000000")


def test_the_survivorship_warning_is_attached(pool, golden):
    assert pool.warnings
    assert pool.master_as_of == date.fromisoformat(golden["roster_as_of"])
