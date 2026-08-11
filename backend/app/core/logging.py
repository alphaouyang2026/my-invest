import logging

import structlog

from app.core.config import get_settings


def configure_logging() -> None:
    """Structured (JSON-renderable) logging. Bind task_id / backtest_run_id via
    structlog.contextvars.bind_contextvars(...) at the start of a task or request
    so every log line emitted underneath carries them automatically."""
    settings = get_settings()
    logging.basicConfig(
        format="%(message)s",
        level=settings.log_level,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(settings.log_level)),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
