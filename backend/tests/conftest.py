"""Tests run against a real Postgres (not SQLite): the spec's fixed-point
money math and jsonb task payloads aren't faithfully emulated by SQLite, and
that's exactly where a false-positive test would hurt most. Point
TEST_DATABASE_URL at a throwaway database (see docker-compose's `db-test`
service) before running these."""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from app.api.data_sync import get_sync_workflow
from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.services.jquants_sync_workflow import JQuantsSyncWorkflow, SyncPolicy
from app.services.quality_rules import QualityPolicy


def _test_database_url() -> str | URL:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit

    repo_env = Path(__file__).resolve().parents[2] / ".env"
    settings = Settings(_env_file=repo_env)
    test_database = os.environ.get("TEST_POSTGRES_DB")
    if test_database is None and repo_env.exists():
        for line in repo_env.read_text(encoding="utf-8").splitlines():
            if line.startswith("TEST_POSTGRES_DB="):
                test_database = line.partition("=")[2].strip()
                break
    return URL.create(
        "postgresql+psycopg",
        username=settings.postgres_user,
        password=settings.postgres_password,
        host="127.0.0.1",
        port=5433,
        database=test_database or "investresearch_test",
    )


TEST_DATABASE_URL = _test_database_url()


@pytest.fixture(scope="session")
def engine():
    engine = create_engine(TEST_DATABASE_URL, connect_args={"connect_timeout": 3})

    # Rebuild from scratch rather than create_all's checkfirst, which would
    # silently keep a stale table or enum from an earlier schema and produce
    # confusing failures far from the cause. Guarded so this can only ever
    # point at a throwaway database.
    name = engine.url.database or ""
    if "test" not in name:
        raise RuntimeError(f"Refusing to reset non-test database {name!r}")
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    Base.metadata.create_all(engine)

    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine):
    """Sessions that share one outer transaction, rolled back afterwards.

    The sync workflow commits many times per run, so `create_savepoint` is
    what keeps those commits real to the code under test while still leaving
    nothing behind — without it each commit would end the test's transaction.
    """
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        yield factory
    finally:
        transaction.rollback()
        connection.close()


@pytest.fixture
def db_session(session_factory) -> Session:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def sync_workflow(session_factory) -> JQuantsSyncWorkflow:
    return JQuantsSyncWorkflow(session_factory)


@pytest.fixture
def make_workflow(session_factory):
    """Build a workflow around a scripted adapter, with the batch size the
    test cares about."""

    def _make(
        adapter,
        *,
        batch_size: int = 5,
        source: str = "jquants",
        quality_policy: QualityPolicy = QualityPolicy(),
    ) -> JQuantsSyncWorkflow:
        return JQuantsSyncWorkflow(
            session_factory,
            adapter,
            policy=SyncPolicy(batch_size=batch_size),
            quality_policy=quality_policy,
            source=source,
        )

    return _make


@pytest.fixture
def client(db_session: Session, sync_workflow: JQuantsSyncWorkflow):
    def _get_db_override():
        yield db_session

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_sync_workflow] = lambda: sync_workflow
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_sync_workflow, None)
