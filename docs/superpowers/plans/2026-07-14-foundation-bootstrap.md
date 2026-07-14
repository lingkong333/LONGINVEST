# Public Foundation Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first server-verifiable foundation slice with typed configuration, a standard live-health response, request IDs, and isolated Docker services.

**Architecture:** Keep framework plumbing under `backend/src/long_invest/platform` and process creation under `bootstrap`/`entrypoints`. PostgreSQL and Redis run as private Compose services; this slice does not introduce business modules or database-owned state.

**Tech Stack:** Python 3.12, FastAPI, Pydantic Settings, pytest, HTTPX, Docker Compose, PostgreSQL 16, Redis 7.

---

## File Map

- `.gitignore`: generated files, secrets, caches, local data.
- `.gitattributes`: stable line endings across Windows and Linux.
- `backend/pyproject.toml`: runtime and test dependencies.
- `backend/src/long_invest/platform/config/settings.py`: typed environment configuration.
- `backend/src/long_invest/platform/http/responses.py`: standard response construction.
- `backend/src/long_invest/platform/http/request_id.py`: request ID validation and middleware.
- `backend/src/long_invest/modules/health/api.py`: liveness endpoint only.
- `backend/src/long_invest/bootstrap/app.py`: FastAPI application factory.
- `backend/src/long_invest/entrypoints/api.py`: ASGI entrypoint.
- `backend/tests/platform/config/test_settings.py`: configuration behavior.
- `backend/tests/modules/health/test_live.py`: health contract and request ID behavior.
- `deploy/docker/backend.Dockerfile`: non-root API image.
- `deploy/compose.yaml`: isolated PostgreSQL, Redis, and API services.
- `.env.example`: non-secret local defaults.

### Task 1: Typed Configuration

**Files:**
- Create: `.gitignore`
- Create: `.gitattributes`
- Create: `backend/pyproject.toml`
- Create: `backend/tests/platform/config/test_settings.py`
- Create: `backend/src/long_invest/platform/config/settings.py`

- [ ] **Step 1: Write the failing configuration tests**

```python
from long_invest.platform.config.settings import AppSettings


def test_settings_use_safe_defaults() -> None:
    settings = AppSettings(_env_file=None)

    assert settings.app_name == "LongInvest"
    assert settings.environment == "development"
    assert settings.api_port == 8000
    assert settings.database_url.startswith("postgresql+")
    assert settings.redis_url.startswith("redis://")


def test_settings_accept_longinvest_environment_prefix(monkeypatch) -> None:
    monkeypatch.setenv("LONGINVEST_ENVIRONMENT", "test")
    monkeypatch.setenv("LONGINVEST_API_PORT", "9000")

    settings = AppSettings(_env_file=None)

    assert settings.environment == "test"
    assert settings.api_port == 9000
```

- [ ] **Step 2: Run the test and verify the missing module failure**

Run: `cd backend && uv run pytest tests/platform/config/test_settings.py -q`

Expected: collection fails because `long_invest.platform.config.settings` does not exist.

- [ ] **Step 3: Implement the minimal typed settings**

```python
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="LONGINVEST_",
        extra="ignore",
    )

    app_name: str = "LongInvest"
    environment: Literal["development", "test", "production"] = "development"
    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8000, ge=1, le=65535)
    database_url: str = "postgresql+asyncpg://longinvest:longinvest@postgres:5432/longinvest"
    redis_url: str = "redis://redis:6379/0"


@lru_cache
def get_settings() -> AppSettings:
    return AppSettings()
```

- [ ] **Step 4: Run the focused tests**

Run: `cd backend && uv run pytest tests/platform/config/test_settings.py -q`

Expected: `2 passed`.

### Task 2: Standard Live Health Contract

**Files:**
- Create: `backend/tests/modules/health/test_live.py`
- Create: `backend/src/long_invest/platform/http/responses.py`
- Create: `backend/src/long_invest/platform/http/request_id.py`
- Create: `backend/src/long_invest/modules/health/api.py`
- Create: `backend/src/long_invest/bootstrap/app.py`
- Create: `backend/src/long_invest/entrypoints/api.py`

- [ ] **Step 1: Write failing API contract tests**

```python
from fastapi.testclient import TestClient

from long_invest.bootstrap.app import create_app


def test_live_health_uses_standard_response() -> None:
    response = TestClient(create_app()).get("/health/live")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["code"] == "OK"
    assert body["message"] == "服务运行正常"
    assert body["data"] == {"status": "live"}
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert body["server_time"].endswith("Z")


def test_live_health_preserves_valid_request_id() -> None:
    response = TestClient(create_app()).get(
        "/health/live",
        headers={"X-Request-ID": "req_01J00000000000000000000000"},
    )

    assert response.headers["X-Request-ID"] == "req_01J00000000000000000000000"
    assert response.json()["request_id"] == "req_01J00000000000000000000000"


def test_live_health_replaces_invalid_request_id() -> None:
    response = TestClient(create_app()).get(
        "/health/live",
        headers={"X-Request-ID": "contains spaces"},
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"].startswith("req_")
    assert response.headers["X-Request-ID"] != "contains spaces"
```

- [ ] **Step 2: Run the test and verify the missing application failure**

Run: `cd backend && uv run pytest tests/modules/health/test_live.py -q`

Expected: collection fails because `long_invest.bootstrap.app` does not exist.

- [ ] **Step 3: Implement request IDs and the standard response**

Use a `ContextVar` for the current request ID, accept only `req_` values containing 8 to 64 ASCII letters, digits, underscore, or hyphen, and generate `req_<uuid hex>` otherwise. The response helper returns `success`, `code`, `message`, `data`, `request_id`, and an aware UTC `server_time` rendered with `Z`.

- [ ] **Step 4: Implement the application factory and health route**

Create a FastAPI app with request ID middleware and one `GET /health/live` route. The entrypoint exports `app = create_app()`.

- [ ] **Step 5: Run the API contract tests**

Run: `cd backend && uv run pytest tests/modules/health/test_live.py -q`

Expected: `3 passed`.

- [ ] **Step 6: Run the full backend suite**

Run: `cd backend && uv run pytest -q`

Expected: `5 passed`.

### Task 3: Linux Container Runtime

**Files:**
- Create: `.env.example`
- Create: `deploy/docker/backend.Dockerfile`
- Create: `deploy/compose.yaml`

- [ ] **Step 1: Add a non-root backend image**

The image uses Python 3.12 slim, installs the backend package, creates an unprivileged `longinvest` user, and starts Uvicorn without development reload.

- [ ] **Step 2: Add isolated Compose services**

Compose defines PostgreSQL 16, Redis 7, and API. PostgreSQL and Redis have health checks and no host port bindings. API binds only `127.0.0.1:18080`, waits for healthy dependencies, uses a read-only root filesystem with `/tmp` as tmpfs, and receives URLs through environment variables.

- [ ] **Step 3: Validate Compose syntax on the server**

Run: `docker compose -f deploy/compose.yaml config --quiet`

Expected: exit code 0.

- [ ] **Step 4: Build and start the stack on the server**

Run: `docker compose -f deploy/compose.yaml up -d --build`

Expected: PostgreSQL and Redis become healthy; API starts.

- [ ] **Step 5: Verify the live endpoint from the server**

Run: `curl --fail --silent http://127.0.0.1:18080/health/live`

Expected: a standard JSON response with `data.status` equal to `live`.

- [ ] **Step 6: Verify container state and logs**

Run: `docker compose -f deploy/compose.yaml ps`

Expected: all three services are running and dependencies are healthy.

Run: `docker compose -f deploy/compose.yaml logs --no-color api`

Expected: no traceback, secret, or repeated restart.

### Task 4: Foundation Bootstrap Verification

- [ ] **Step 1: Run all tests in a clean backend environment**

Run: `cd backend && uv run pytest -q`

Expected: all tests pass with no warnings.

- [ ] **Step 2: Rebuild without cache-dependent local state**

Run: `docker compose -f deploy/compose.yaml build api`

Expected: exit code 0.

- [ ] **Step 3: Record the implementation result**

Update this checklist and report the exact test count, running services, endpoint result, and any server limitation observed. Do not begin the logging/audit slice until every verification above passes.
