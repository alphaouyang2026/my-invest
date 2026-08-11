"""Dump the FastAPI app's OpenAPI schema to a file, without needing a running
server or a database connection. The frontend's `npm run generate:api`
consumes this to regenerate its typed API client — run this first whenever
backend routes change."""

import json
import sys
from pathlib import Path

from app.main import app

DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "openapi.json"


def main() -> None:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    output.write_text(json.dumps(app.openapi(), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote OpenAPI schema to {output}")


if __name__ == "__main__":
    main()
