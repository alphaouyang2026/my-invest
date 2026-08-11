# Dev-oriented image: source is bind-mounted in by docker-compose, this build
# just bakes the venv (see the `backend_venv` named volume in docker-compose.yml
# for why a fresh volume still gets this venv on first run).
FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

COPY . .

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
