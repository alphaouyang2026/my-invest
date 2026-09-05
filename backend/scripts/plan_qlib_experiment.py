"""Validate and persist the common fold/trial plan; never train."""
from app.experiments.search_cli import main

if __name__ == "__main__":
    raise SystemExit(main("plan"))
