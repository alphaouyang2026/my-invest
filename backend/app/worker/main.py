from app.core.logging import configure_logging
from app.db.session import get_engine, get_sessionmaker
from app.worker.runner import run_forever
from app.worker import tasks as _tasks  # noqa: F401 — registers business handlers

if __name__ == "__main__":
    configure_logging()
    run_forever(get_sessionmaker(), get_engine())
