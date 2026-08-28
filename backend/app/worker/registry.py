from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.models.task import Task

TaskHandler = Callable[[dict[str, Any]], dict[str, Any] | None]

# What to do with one of this task type's rows when a worker died while it was
# RUNNING. It receives a task the runner has already marked FAILED, and is free
# to overwrite that outcome — a task with a persisted checkpoint may be put back
# on the queue instead. Its real job is the *business* row: whatever the handler
# left in an active state must be moved to a terminal one, or the screen that
# reports it becomes a dead end.
TaskRecovery = Callable[[Session, Task], None]

_handlers: dict[str, TaskHandler] = {}
_recoveries: dict[str, TaskRecovery] = {}


def register(
    task_type: str, *, recover: TaskRecovery | None = None
) -> Callable[[TaskHandler], TaskHandler]:
    """Decorator: registers a handler, and its crash recovery, for a task_type.

    The two belong together. A handler that puts a business row into an active
    state owns the question of what happens to that row when the process dies
    mid-flight, and answering it anywhere else is how the answer goes missing:
    the runner cannot tell a task type that needs no recovery from one whose
    author forgot, so it says so out loud instead of guessing.
    """

    def decorator(fn: TaskHandler) -> TaskHandler:
        _handlers[task_type] = fn
        if recover is not None:
            _recoveries[task_type] = recover
        return fn

    return decorator


def get_handler(task_type: str) -> TaskHandler | None:
    return _handlers.get(task_type)


def get_recovery(task_type: str) -> TaskRecovery | None:
    return _recoveries.get(task_type)
