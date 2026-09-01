# Dev-oriented image: source is bind-mounted in by docker-compose, this build
# just bakes the venv (see the anonymous `/app/.venv` volume in
# docker-compose.yml for why the bind mount does not hide it).
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*
 
RUN pip install --no-cache-dir uv

WORKDIR /app

# The uv cache and the sync target sit on different filesystems here, so uv
# cannot hardlink and warns on every sync. Copying is what it falls back to
# anyway; saying so explicitly just drops the warning.
ENV UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

COPY . .

# --reload-dir keeps the watcher off the rest of the bind mount: pytest drops
# 0o700 basetemp dirs in here (see pyproject addopts) that the watcher cannot
# traverse, and the resulting IO error kills the reloader supervisor.
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "app"]
