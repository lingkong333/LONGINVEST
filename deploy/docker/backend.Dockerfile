FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN groupadd --system longinvest \
    && useradd --system --gid longinvest --home-dir /app longinvest

WORKDIR /app

COPY backend/pyproject.toml ./pyproject.toml
COPY backend/uv.lock ./uv.lock

RUN uv sync --frozen --no-dev --no-install-project

COPY backend/src ./src

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

FROM base AS runtime

USER longinvest

EXPOSE 8000

CMD ["uvicorn", "long_invest.entrypoints.api:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS test

RUN uv sync --frozen --extra dev

COPY backend/tests ./tests

CMD ["pytest", "-q"]
