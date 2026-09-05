"""Regenerate reports solely from verified trial artifacts."""
from app.experiments.search_cli import main

if __name__ == "__main__":
    raise SystemExit(main("report"))
