# 01 — 项目脚手架与基础设施

**What to build:** 建立可通过一条命令启动的前端、后端、PostgreSQL 三层项目骨架（Docker Compose，前端和后端仅监听 localhost），具备可运行的数据库迁移、结构化日志和后台任务框架（PostgreSQL 任务表 + 单个工作进程串行执行，其余任务排队），为后续所有纵切工作提供落地基础。这是前置的基础设施票据，本身不是一个纵切但阻塞几乎所有后续工作。

**Blocked by:** None — can start immediately

**Status:** ready-for-agent

- [ ] `docker compose up` 启动前端、后端、PostgreSQL 三个服务，前端和后端仅绑定 localhost
- [ ] 数据库迁移脚本可运行并可回滚，迁移记录持久化
- [ ] 后端日志为结构化日志，预留任务 ID / 回测运行 ID 字段
- [ ] 任务表存在，支持创建/查询任务状态；单工作进程串行执行任务，其余任务排队（无实际业务任务，仅框架验证）
- [ ] 不引入 Redis、Kafka、独立对象存储或分布式任务系统
- [ ] 后端和前端各有可运行的测试框架搭建（pytest、前端单元测试），即使初期测试很少

## Design decisions

Settled via `/grilling` session before implementation.

**Repo & version control**
- `git init` now, first commit captures the scaffold
- Layout: `frontend/`, `backend/`, `docker-compose.yml` at root, `backend/migrations/` for Alembic

**Backend**
- Python 3.12 (pinned in the Docker image, not the host's Python) + FastAPI + SQLAlchemy 2.0 (sync, psycopg3 driver) + Alembic
- Route handlers touching the DB stay `def` (threadpooled by FastAPI), not `async def`
- `uv` for dependency management
- `structlog` for structured logging (task ID / backtest run ID carried via context)

**Frontend**
- React / Next.js (App Router) + TypeScript, `npm`
- Vitest + React Testing Library for unit tests; Playwright e2e deferred to ticket 10 (first ticket with a real user flow to exercise)

**Task framework**
- Table columns: `id, task_type, status(queued/running/succeeded/failed/cancelled), payload(jsonb), progress(jsonb), error, created_at, started_at, finished_at, updated_at`
- Separate worker process/container, polling on an interval (not Postgres `LISTEN`/`NOTIFY` — no low-latency requirement, and polling is more robust across worker restarts)
- On worker startup, any task orphaned in `running` status is marked `failed` (never silently resumed — safe resumption would need checkpointing that doesn't exist yet)

**Database**
- Postgres data in a named volume (`pgdata`), survives `docker compose down`; only `down -v` wipes it, and that should be a deliberate action
- Backend tests run against a real Postgres (not SQLite — SQLite doesn't faithfully emulate `NUMERIC`/`jsonb` behavior the spec's money math and task payloads depend on), each test wrapped in a rolled-back transaction

**Connectivity**
- Server-side Next.js calls hit `http://backend:8000` (docker-internal hostname); browser calls use `NEXT_PUBLIC_API_URL=http://localhost:8000`
- Typed TS client generated from FastAPI's OpenAPI schema via an on-demand script (e.g. `openapi-typescript`), so later tickets adding endpoints just regenerate it

**Docker**
- Docker Desktop + WSL2 are not installed on the dev machine as of this writing; the user installs them separately. The scaffold targets Docker Compose directly — no non-Docker fallback dev path.
