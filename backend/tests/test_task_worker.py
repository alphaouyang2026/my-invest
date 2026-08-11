from datetime import datetime, timezone

from sqlalchemy.orm import sessionmaker

from app.models.task import Task, TaskStatus
from app.worker.registry import get_handler, register
from app.worker.runner import recover_orphaned_tasks


def test_recover_orphaned_tasks_marks_running_as_failed(engine):
    """Uses its own session (not the rolled-back `db_session` fixture) since
    recover_orphaned_tasks commits internally; cleans up what it inserts."""
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as setup_session:
        task = Task(
            task_type="noop",
            status=TaskStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        setup_session.add(task)
        setup_session.commit()
        task_id = task.id

    try:
        recovered = recover_orphaned_tasks(session_factory)
        assert recovered == 1

        with session_factory() as check_session:
            reloaded = check_session.get(Task, task_id)
            assert reloaded.status == TaskStatus.FAILED
            assert reloaded.error is not None
    finally:
        with session_factory() as cleanup_session:
            cleanup_session.query(Task).filter(Task.id == task_id).delete()
            cleanup_session.commit()


def test_registered_handler_is_retrievable():
    calls = []

    @register("smoke_test_task")
    def handler(payload: dict) -> dict:
        calls.append(payload)
        return {"ok": True}

    assert get_handler("smoke_test_task") is handler
    handler({"x": 1})
    assert calls == [{"x": 1}]
