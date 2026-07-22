FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install --no-install-recommends --yes git \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system longinvest \
    && useradd --system --gid longinvest --home-dir /app longinvest \
    && mkdir -p /var/log/longinvest /var/lib/long-invest/strategies \
    && chown -R longinvest:longinvest /var/log/longinvest /var/lib/long-invest

WORKDIR /app

COPY backend/pyproject.toml ./pyproject.toml
COPY backend/uv.lock ./uv.lock

RUN uv sync --frozen --no-dev --no-install-project

COPY backend/src ./src
COPY backend/alembic.ini ./alembic.ini
COPY backend/alembic ./alembic

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

FROM base AS runtime

USER longinvest

EXPOSE 8000

CMD ["uvicorn", "long_invest.entrypoints.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

FROM base AS test

RUN uv sync --frozen --extra dev

COPY backend/tests ./tests
COPY backend/openapi.json ./openapi.json
COPY deploy/compose.yaml /deploy/compose.yaml
COPY deploy/data/trading-calendar /deploy/data/trading-calendar
COPY deploy/docker/strategy-runner.Dockerfile /deploy/docker/strategy-runner.Dockerfile
COPY deploy/security/strategy-runner-seccomp.json /deploy/security/strategy-runner-seccomp.json

CMD ["pytest", "-q"]
