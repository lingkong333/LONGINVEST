# QFQ Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement monitored-stock QFQ dataset refresh with frozen windows, validation, atomic current-version switching, failure isolation, public APIs, and a dedicated worker.

**Architecture:** A new `qfq` module owns immutable dataset metadata, normalized dataset bars, refresh runs, validation, switching, and events. It consumes security, daily-bar, Provider, job, and outbox capabilities only through public contracts; external fetches occur outside database transactions and successful switches commit dataset, run, and event atomically.

**Tech Stack:** Python 3.13, FastAPI, Pydantic, SQLAlchemy 2, Alembic, PostgreSQL 16, Redis/RQ, Docker Compose, Pytest, Ruff, OpenAPI TypeScript

---

## File structure

- Create `backend/src/long_invest/modules/qfq/contracts.py`: stable enums, commands, views, and errors used at module boundaries.
- Create `backend/src/long_invest/modules/qfq/validation.py`: pure QFQ window validation and deterministic checksum.
- Create `backend/src/long_invest/modules/qfq/models.py`: dataset, bar, and refresh-run persistence.
- Create `backend/src/long_invest/modules/qfq/repository.py`: QFQ-only reads, locks, inserts, state changes, and pagination.
- Create `backend/src/long_invest/modules/qfq/outbox.py`: transactional completed/failed event writer.
- Create `backend/src/long_invest/modules/qfq/service.py`: domain state transitions and atomic activation.
- Create `backend/src/long_invest/modules/qfq/application.py`: public orchestration, job submission, Provider call, and transaction boundaries.
- Create `backend/src/long_invest/modules/qfq/api.py`: authenticated query and verified refresh endpoints.
- Create `backend/alembic/versions/20260716_0009_qfq_refresh.py`: one serial migration after `20260715_0008`.
- Modify `backend/src/long_invest/modules/securities/contracts.py` and `application.py`: publish immutable `SecurityIdentity` instead of exposing internal models to QFQ.
- Modify `backend/src/long_invest/modules/daily_data/contracts.py` and `application.py`: publish immutable `DailyBarSnapshot` for the target-date gate.
- Modify `backend/src/long_invest/bootstrap/jobs.py`, `platform/jobs/worker.py`, `bootstrap/app.py`, `alembic/env.py`, and `deploy/compose.yaml`: serial shared integration.
- Regenerate `backend/openapi.json` and `frontend/src/shared/api/generated/schema.d.ts` only after routes are final.

### Task 1: Freeze public contracts and pure validation

**Files:**
- Create: `backend/src/long_invest/modules/qfq/__init__.py`
- Create: `backend/src/long_invest/modules/qfq/contracts.py`
- Create: `backend/src/long_invest/modules/qfq/validation.py`
- Create: `backend/tests/modules/qfq/test_contracts.py`
- Create: `backend/tests/modules/qfq/test_validation.py`
- Modify: `backend/src/long_invest/modules/securities/contracts.py`
- Modify: `backend/src/long_invest/modules/securities/application.py`
- Modify: `backend/src/long_invest/modules/daily_data/contracts.py`
- Modify: `backend/src/long_invest/modules/daily_data/application.py`

- [ ] **Step 1: Write failing contract tests**

Add tests that construct a frozen refresh command and reject invalid windows:

```python
def test_refresh_command_freezes_window_and_input_versions() -> None:
    command = RefreshQfq(
        security_id=uuid4(),
        symbol="600000.SH",
        start=date(2022, 1, 1),
        end=date(2026, 7, 16),
        as_of_date=date(2026, 7, 16),
        input_daily_version=3,
        trigger_reason="MANUAL",
        request_id="req-qfq-1",
        idempotency_key="qfq-1",
        actor_user_id=str(uuid4()),
    )
    assert command.start <= command.as_of_date == command.end
    assert command.input_daily_version == 3


def test_refresh_command_rejects_unbounded_or_future_window() -> None:
    with pytest.raises(ValueError, match="window"):
        RefreshQfq(
            security_id=uuid4(), symbol="600000.SH",
            start=date(2026, 7, 17), end=date(2026, 7, 16),
            as_of_date=date(2026, 7, 16), input_daily_version=1,
            trigger_reason="MANUAL", request_id="req-qfq-1",
            idempotency_key="qfq-1", actor_user_id=str(uuid4()),
        )
```

Add public-contract tests proving `SecurityIdentity` and `DailyBarSnapshot` contain only immutable scalar facts required by QFQ.

- [ ] **Step 2: Run the tests and verify the new contracts are missing**

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/modules/qfq/test_contracts.py backend/tests/modules/qfq/test_validation.py -q`

Expected: collection fails because the `qfq` contracts do not exist.

- [ ] **Step 3: Implement stable enums and immutable commands**

Define these exact state values:

```python
class QfqDatasetLifecycle(StrEnum):
    STAGING = "STAGING"
    CURRENT = "CURRENT"
    SUPERSEDED = "SUPERSEDED"


class QfqFreshness(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"


class QfqRefreshStatus(StrEnum):
    PENDING = "PENDING"
    FETCHING = "FETCHING"
    VALIDATING = "VALIDATING"
    COMMITTING = "COMMITTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    SUPERSEDED = "SUPERSEDED"
```

Implement frozen `RefreshQfq`, `QfqBarInput`, `ValidatedQfqWindow`, `QfqDatasetView`, `QfqRefreshView`, `SecurityIdentity`, and `DailyBarSnapshot`. Validate symbol format, `start <= as_of_date == end`, positive input versions, nonblank request/audit fields, positive prices, valid OHLC, and nonnegative quantities.

- [ ] **Step 4: Implement pure validation and checksum**

`validate_qfq_window(command, bars, daily_snapshot)` must:

```python
ordered = tuple(sorted(bars, key=lambda item: item.trade_date))
if not ordered:
    raise QfqValidationError("QFQ_EMPTY_RESULT")
if len({item.trade_date for item in ordered}) != len(ordered):
    raise QfqValidationError("QFQ_DUPLICATE_DATE")
if ordered[0].trade_date < command.start or ordered[-1].trade_date != command.end:
    raise QfqValidationError("QFQ_WINDOW_INCOMPLETE")
if daily_snapshot.trade_date != command.end:
    raise QfqValidationError("QFQ_DAILY_GATE_NOT_MET")
```

Compare the final QFQ close with the same-date unadjusted close using exact normalized decimals, then hash canonical UTF-8 JSON with SHA-256. The canonical rows must be date-ascending and decimal strings must use fixed non-exponent form.

- [ ] **Step 5: Publish security and daily snapshots**

Add `SecurityApplication.resolve_identity(symbol) -> SecurityIdentity` and `DailyDataApplication.snapshot(symbol, trade_date) -> DailyBarSnapshot | None`. These methods may use their own repositories internally, but QFQ imports only the public dataclasses and application methods.

- [ ] **Step 6: Run focused tests and commit**

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/modules/qfq backend/tests/modules/securities backend/tests/modules/daily_data/test_contracts.py -q`

Expected: PASS.

Commit:

```text
git add backend/src/long_invest/modules/qfq backend/src/long_invest/modules/securities backend/src/long_invest/modules/daily_data backend/tests/modules/qfq backend/tests/modules/securities backend/tests/modules/daily_data/test_contracts.py
git commit -m "feat: define qfq refresh contracts"
```

### Task 2: Add QFQ persistence and the serial migration

**Files:**
- Create: `backend/src/long_invest/modules/qfq/models.py`
- Create: `backend/alembic/versions/20260716_0009_qfq_refresh.py`
- Create: `backend/tests/modules/qfq/test_models.py`
- Create: `backend/tests/integration/test_qfq_migration.py`
- Modify: `backend/alembic/env.py`

- [ ] **Step 1: Write failing model and migration tests**

Assert the three tables, named constraints, foreign keys, indexes, and one-current-dataset partial unique index:

```python
def test_qfq_dataset_enforces_one_current_dataset_per_security() -> None:
    index = next(i for i in QfqDataset.__table__.indexes if i.name == "uq_qfq_dataset_current_security")
    assert index.unique is True
    assert "lifecycle = 'CURRENT'" in str(index.dialect_options["postgresql"]["where"])


def test_qfq_bar_primary_key_is_dataset_and_trade_date() -> None:
    assert [column.name for column in QfqDatasetBar.__table__.primary_key.columns] == [
        "dataset_id", "trade_date"
    ]
```

The integration test must upgrade from `20260715_0008`, inspect all constraints and application-role privileges, downgrade to `0008`, and upgrade again.

- [ ] **Step 2: Run tests and confirm table definitions are absent**

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/modules/qfq/test_models.py backend/tests/integration/test_qfq_migration.py -q`

Expected: FAIL because QFQ models and migration `0009` do not exist.

- [ ] **Step 3: Implement the models**

Create:

```python
class QfqDataset(Base):
    __tablename__ = "qfq_dataset"
    # UUID id; security_id FK security.id; symbol; per-security version;
    # requested/actual dates; as_of_date; provider; provider_contract_version;
    # anchor_date/anchor_close; row_count; checksum;
    # lifecycle; freshness; stale_reason; created/activated/superseded timestamps.


class QfqDatasetBar(Base):
    __tablename__ = "qfq_dataset_bar"
    # composite PK dataset_id/trade_date; OHLC Numeric(18,6);
    # volume BigInteger; amount Numeric(24,4); FK CASCADE to qfq_dataset.


class QfqRefreshRun(Base):
    __tablename__ = "qfq_refresh_run"
    # job_id unique; security/window/input version/request hash;
    # status, stage timestamps, candidate/activated dataset IDs,
    # row_count/checksum/error code/retryable/created/updated/completed.
```

Use explicit check constraints for lifecycle, freshness, run status, dates, row count, checksum length, OHLC, and quantities. Use `SELECT FOR UPDATE` compatibility indexes on `(security_id, lifecycle)` and refresh history on `(security_id, created_at)`.

- [ ] **Step 4: Implement migration `20260716_0009`**

Set `down_revision = "20260715_0008"`. Create tables in dataset, bar, run order; create the partial unique index; grant application role `SELECT/INSERT/UPDATE/DELETE` only as required; downgrade in reverse dependency order. Do not alter prior migration files.

- [ ] **Step 5: Register models and verify migration**

Import all three models in `alembic/env.py` and run:

`backend/.venv/Scripts/alembic.exe heads`

Expected: one head, `20260716_0009`.

Run offline upgrade and downgrade SQL generation, then run the real migration test in the server test Compose environment.

- [ ] **Step 6: Commit**

```text
git add backend/alembic backend/src/long_invest/modules/qfq/models.py backend/tests/modules/qfq/test_models.py backend/tests/integration/test_qfq_migration.py
git commit -m "feat: persist qfq datasets"
```

### Task 3: Implement repository, events, and atomic switching

**Files:**
- Create: `backend/src/long_invest/modules/qfq/repository.py`
- Create: `backend/src/long_invest/modules/qfq/outbox.py`
- Create: `backend/src/long_invest/modules/qfq/service.py`
- Create: `backend/tests/modules/qfq/test_repository.py`
- Create: `backend/tests/modules/qfq/test_service.py`
- Create: `backend/tests/integration/test_qfq_atomic_switch.py`

- [ ] **Step 1: Write failing service tests**

Cover first activation, replacement, stale-on-failure, duplicate checksum, superseded input, and event payload:

```python
async def test_failed_refresh_keeps_current_dataset_and_marks_it_stale() -> None:
    current = await fixture.activate(version=1)
    result = await service.fail(run_id, code="QFQ_PROVIDER_FAILED", retryable=True)
    assert result.current_dataset_id == current.id
    assert result.freshness is QfqFreshness.STALE
    assert events.last.topic == "qfq_refresh.failed"


async def test_success_switches_dataset_and_event_in_one_transaction() -> None:
    first = await fixture.activate(version=1)
    second = await service.activate(run_id, validated_window)
    assert second.version == 2
    assert await repository.lifecycle(first.id) is QfqDatasetLifecycle.SUPERSEDED
    assert events.last.topic == "qfq_refresh.completed"
```

- [ ] **Step 2: Confirm tests fail before implementation**

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/modules/qfq/test_repository.py backend/tests/modules/qfq/test_service.py -q`

Expected: FAIL because repository and service do not exist.

- [ ] **Step 3: Implement QFQ-only repository methods**

Provide methods for run creation/replay, run transition, current dataset lock, next version, dataset/bar insert, lifecycle/freshness update, current metadata, paginated current bars, refresh history, and cleanup candidates. Every update must include the expected prior status; a zero-row state transition raises `QFQ_REFRESH_CONFLICT`.

- [ ] **Step 4: Implement transactional event adapter**

Write `qfq_refresh.completed` or `qfq_refresh.failed` into `event_outbox` using stable dedupe keys `qfq:{run_id}:completed` and `qfq:{run_id}:failed`. Payloads contain only the fields listed in the design and never contain raw Provider responses.

- [ ] **Step 5: Implement domain service transitions**

`QfqRefreshService.activate()` must lock the current row, recheck `input_daily_version`, insert the immutable candidate and bars, supersede the old row, promote the candidate, complete the run, and write the event through the injected event port. `fail()` marks the current dataset stale without changing its lifecycle and records the failed event.

- [ ] **Step 6: Run atomic integration tests**

Use a real PostgreSQL transaction and inject a failing outbox writer. Prove that dataset, bars, current pointer, run state, and event all commit together or all roll back. Also run two concurrent activations and prove only one becomes current.

- [ ] **Step 7: Commit**

```text
git add backend/src/long_invest/modules/qfq backend/tests/modules/qfq backend/tests/integration/test_qfq_atomic_switch.py
git commit -m "feat: switch qfq datasets atomically"
```

### Task 4: Add application orchestration and public APIs

**Files:**
- Create: `backend/src/long_invest/modules/qfq/application.py`
- Create: `backend/src/long_invest/modules/qfq/api.py`
- Create: `backend/tests/modules/qfq/test_application.py`
- Create: `backend/tests/modules/qfq/test_api.py`

- [ ] **Step 1: Write failing application and API tests**

Test authenticated reads, verified writes, explicit window validation, confirmation, required idempotency header, 202 response, nonempty typed records, and stable errors:

```python
def test_refresh_returns_accepted_job() -> None:
    response = client.post(
        "/api/v1/qfq-data/600000.SH/refresh",
        json={
            "start": "2022-01-01", "end": "2026-07-16",
            "as_of_date": "2026-07-16", "confirm": True,
            "reason": "manual refresh",
        },
        headers={"Idempotency-Key": "qfq-refresh-1"},
    )
    assert response.status_code == 202
    assert response.json()["code"] == "JOB_ACCEPTED"
    assert response.json()["data"]["job_type"] == "QFQ_REFRESH"
```

- [ ] **Step 2: Confirm route tests fail**

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/modules/qfq/test_application.py backend/tests/modules/qfq/test_api.py -q`

Expected: FAIL because application and router do not exist.

- [ ] **Step 3: Implement job submission**

`QfqApplication.submit_refresh()` must call public security and daily snapshot methods, freeze their IDs and versions, and submit:

```python
SubmitJob(
    job_type="QFQ_REFRESH",
    queue="qfq-refresh",
    idempotency_scope=f"qfq-refresh:{security.id}",
    idempotency_key=idempotency_key,
    request_id=request_id,
    config_snapshot=frozen_config,
    business_object_type="security",
    business_object_id=str(security.id),
    created_by_user_id=actor_user_id,
    soft_timeout_seconds=240,
    hard_timeout_seconds=300,
)
```

Submit the job and high-risk audit event in one database transaction. Same key/same content returns the existing job; same key/different content returns 409.

- [ ] **Step 4: Implement typed query and refresh routes**

`GET` returns a concrete success envelope containing dataset metadata, freshness, rows, and pagination. `POST` requires verified write identity, confirmation, reason, and a manually validated idempotency dependency so OpenAPI marks the header required while missing values return `IDEMPOTENCY_KEY_REQUIRED`.

- [ ] **Step 5: Run focused tests and commit**

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/modules/qfq -q`

Expected: PASS.

```text
git add backend/src/long_invest/modules/qfq backend/tests/modules/qfq
git commit -m "feat: expose qfq refresh api"
```

### Task 5: Execute refresh jobs on an isolated worker

**Files:**
- Modify: `backend/src/long_invest/bootstrap/jobs.py`
- Modify: `backend/src/long_invest/platform/jobs/worker.py`
- Modify: `deploy/compose.yaml`
- Modify: `backend/tests/integration/test_worker_queue_isolation.py`
- Create: `backend/tests/integration/test_qfq_refresh_job.py`

- [ ] **Step 1: Write failing handler and queue tests**

Assert `QFQ_REFRESH` is registered, the handler passes the exact frozen `HISTORICAL_DAILY_QFQ` request to Provider, and Compose has `worker-qfq-refresh` listening only to `qfq-refresh`.

```python
assert services["worker-qfq-refresh"]["environment"]["LONGINVEST_WORKER_QUEUES"] == "qfq-refresh"
assert "qfq-refresh" not in services["worker-daily-market-data"]["environment"]["LONGINVEST_WORKER_QUEUES"]
assert HANDLERS["QFQ_REFRESH"] is qfq_refresh
```

- [ ] **Step 2: Confirm tests fail before registration**

Run: `backend/.venv/Scripts/python.exe -m pytest backend/tests/integration/test_qfq_refresh_job.py backend/tests/integration/test_worker_queue_isolation.py -q`

Expected: FAIL because the handler and worker service are absent.

- [ ] **Step 3: Implement the handler**

Parse and validate the frozen config before external calls. Fetch QFQ bars through `ProviderService.daily_bars()` with `ProviderCapability.HISTORICAL_DAILY_QFQ`; call pure validation; persist failure or activate success through `QfqApplication`. Map Provider/transient timeouts to stable retryable results and content errors to nonretryable results. Never open a database transaction around the HTTP request.

- [ ] **Step 4: Register handler and Compose worker**

Register `HANDLERS["QFQ_REFRESH"] = qfq_refresh`. Add `worker-qfq-refresh` by following existing read-only, no-new-privileges, memory-limited worker settings and set only `LONGINVEST_WORKER_QUEUES: qfq-refresh`.

- [ ] **Step 5: Verify recovery and isolation**

Test soft timeout, stale fence, Provider failure, Worker restart recovery, duplicate delivery, and a successful refresh while realtime and daily queue tests continue to pass.

- [ ] **Step 6: Commit**

```text
git add backend/src/long_invest/bootstrap/jobs.py backend/src/long_invest/platform/jobs/worker.py deploy/compose.yaml backend/tests/integration
git commit -m "feat: execute qfq refresh jobs"
```

### Task 6: Serial integration, generated types, and container acceptance

**Files:**
- Modify: `backend/src/long_invest/bootstrap/app.py`
- Modify: `backend/openapi.json`
- Modify: `frontend/src/shared/api/generated/schema.d.ts`
- Modify: `backend/tests/integration/test_stage2_batch1.py`

- [ ] **Step 1: Add failing main-app route and schema tests**

Assert both QFQ paths are registered and their 200/202 success responses reference concrete schemas. Assert the refresh operation publishes required `Idempotency-Key`.

- [ ] **Step 2: Register the router and regenerate contracts**

Include only `qfq.api.router` in `bootstrap/app.py`. Export runtime OpenAPI to `backend/openapi.json`, run `npm run generate:api`, and prove runtime OpenAPI equals the saved file with unique operation IDs.

- [ ] **Step 3: Run backend verification**

Run QFQ, Provider, daily-data, job, outbox, migration, and stage-2 integration tests. Run Ruff, compileall, `git diff --check`, `alembic heads`, offline upgrade/downgrade SQL, and real server test-profile migration tests.

Expected: one migration head and all relevant tests pass.

- [ ] **Step 4: Run frontend verification**

Run:

```text
npm test -- --run
npm run lint
npm run typecheck
npm run build
```

Expected: all commands pass and QFQ success responses are concrete TypeScript types.

- [ ] **Step 5: Deploy and verify containers**

Push `main`, run server `docker compose -f deploy/compose.yaml up -d --build`, and verify PostgreSQL, Redis, API, frontend, dispatcher, watchdog, realtime, daily, maintenance, and QFQ workers. Check migration `20260716_0009`, `/health/ready`, QFQ unauthenticated 401 behavior, worker queue logs, restart counts, and no new public database or API ports.

- [ ] **Step 6: Final commit if generated files changed after the prior commit**

```text
git add backend/src/long_invest/bootstrap/app.py backend/openapi.json frontend/src/shared/api/generated/schema.d.ts backend/tests/integration/test_stage2_batch1.py
git commit -m "chore: publish qfq api schema"
git push server main
```
