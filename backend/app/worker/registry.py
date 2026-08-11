from collections.abc import Callable
from typing import Any

TaskHandler = Callable[[dict[str, Any]], dict[str, Any] | None]

_handlers: dict[str, TaskHandler] = {}


def register(task_type: str) -> Callable[[TaskHandler], TaskHandler]:
    """Decorator: registers a handler for a task_type. No business tasks exist
    yet in this ticket — this registry exists so later tickets (data sync,
    backtests, ...) just add a handler here instead of touching the runner."""

    def decorator(fn: TaskHandler) -> TaskHandler:
        _handlers[task_type] = fn
        return fn

    return decorator


def get_handler(task_type: str) -> TaskHandler | None:
    return _handlers.get(task_type)
