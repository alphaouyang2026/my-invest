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

### Backend

```bash
cd backend
uv sync
uv run pytest
```

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
