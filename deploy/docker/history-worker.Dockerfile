FROM mcr.microsoft.com/playwright:v1.61.0-noble

COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install --no-install-recommends --yes git \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /app /var/log/longinvest \
    && chown -R pwuser:pwuser /app /var/log/longinvest

WORKDIR /app

COPY backend/pyproject.toml ./pyproject.toml
COPY backend/uv.lock ./uv.lock

RUN uv sync --frozen --no-dev --extra collector --no-install-project

COPY backend/src ./src

RUN uv sync --frozen --no-dev --extra collector \
    && chown -R pwuser:pwuser /app

ENV PATH="/app/.venv/bin:$PATH"

USER pwuser

CMD ["python", "-m", "long_invest.entrypoints.worker"]
