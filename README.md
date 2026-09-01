# 个人日本股票研究与模拟交易系统

Personal research and simulated-trading system for Tokyo Stock Exchange Prime Market
equities. History-based research, backtesting, and hidden-future-data replay only —
no live market data, no broker connectivity, no real order execution.

See [docs/personal-investment-research-system-spec.md](docs/personal-investment-research-system-spec.md)
for the full spec, and [.scratch/personal-investment-research-system/issues/](.scratch/personal-investment-research-system/issues/)
for the implementation tickets.

## Development

Requires Docker Desktop (with WSL2 backend on Windows).

```bash
cp .env.example .env   # fill in secrets
docker compose up
```

- Frontend: http://localhost:3000
- Backend API: http://localhost:8000
- Postgres: `localhost:5432` (dev), `localhost:5433` (throwaway test database)

Migrations are applied automatically on startup by the one-shot `migrate`
service; `backend` and `worker` wait for it to finish.

### Backend

Tests run against the real `db-test` Postgres service, so bring the stack up first:

```bash
cd backend
uv sync
uv run pytest
```

Or inside the container:

```bash
docker compose exec backend sh -c \
  'TEST_DATABASE_URL="postgresql+psycopg://$POSTGRES_USER:$POSTGRES_PASSWORD@db-test:5432/$TEST_POSTGRES_DB" uv run pytest'
```

### Migrations

Schema migrations start from **SQLAlchemy model autogeneration** — don't hand-write
tables, columns, indexes, or constraints:

```bash
docker compose exec backend uv run alembic revision --autogenerate -m "what changed"
docker compose exec backend uv run alembic upgrade head
```

Native PostgreSQL enum lifecycle changes are the exception because Alembic cannot
autogenerate them. Add new labels with an explicit `ALTER TYPE` revision; keep it
separate when a following revision must use the label, because PostgreSQL requires
the enum change to commit first. Autogenerate also doesn't emit `DROP TYPE`; add it
to `downgrade()` by hand whenever a migration creates a native enum.

### Frontend

```bash
cd frontend
npm install
npm run test
```

### Regenerating the typed API client

Whenever backend routes change:

```bash
cd backend && uv run python -m app.export_openapi
cd ../frontend && npm run generate:api
```
