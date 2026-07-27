FROM mcr.microsoft.com/playwright:v1.61.0-noble

COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install --no-install-recommends --yes git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 999 --gid pwuser --home-dir /app --no-create-home longinvest \
    && mkdir -p /app /var/log/longinvest \
    && chown -R longinvest:pwuser /app /var/log/longinvest

WORKDIR /app

COPY backend/pyproject.toml ./pyproject.toml
COPY backend/uv.lock ./uv.lock

RUN uv sync --frozen --no-dev --extra collector --no-install-project

RUN /app/.venv/bin/playwright install chrome \
    && rm -rf /var/lib/apt/lists/*

COPY backend/src ./src

RUN uv sync --frozen --no-dev --extra collector \
    && chown -R longinvest:pwuser /app

ENV PATH="/app/.venv/bin:$PATH"
ENV HOME="/tmp"

USER longinvest

CMD ["python", "-m", "long_invest.entrypoints.worker"]
