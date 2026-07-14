FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system longinvest \
    && useradd --system --gid longinvest --home-dir /app longinvest

WORKDIR /app

COPY backend/pyproject.toml ./pyproject.toml
COPY backend/src ./src

RUN python -m pip install .

USER longinvest

EXPOSE 8000

CMD ["uvicorn", "long_invest.entrypoints.api:app", "--host", "0.0.0.0", "--port", "8000"]

