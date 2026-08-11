from app.core.logging import configure_logging
from app.db.session import get_sessionmaker
from app.worker.runner import run_forever

if __name__ == "__main__":
    configure_logging()
    run_forever(get_sessionmaker())
