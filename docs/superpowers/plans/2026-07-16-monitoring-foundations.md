# Monitoring Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立监控分组、明确时间调度、独立持仓历史、监控订阅和调度执行记录，为四档目标和信号状态机提供可靠的阶段 3 基础。

**Architecture:** 主流程先串行冻结四个领域的契约、ORM 模型和单一 Alembic 迁移，再将分组、调度定义、持仓三个不共享数据表的模块分派到独立工作区并行施工。三项逐个接入后，主流程串行实现监控订阅和调度执行器，最后统一注册路由、导出 OpenAPI、生成前端类型并执行真实 PostgreSQL 与 Docker 验收。

**Tech Stack:** Python 3.12+、FastAPI、Pydantic、SQLAlchemy 2、Alembic、PostgreSQL 16、Redis/RQ、pytest、Ruff、React OpenAPI 类型生成、Docker Compose。

---

## 文件结构与所有权

主流程串行维护：

- `backend/alembic/env.py`
- `backend/alembic/versions/20260716_0010_monitoring_foundations.py`
- `backend/src/long_invest/bootstrap/app.py`
- `backend/src/long_invest/entrypoints/monitor_scheduler.py`
- `deploy/compose.yaml`
- `backend/openapi.json`
- `frontend/src/shared/api/generated/schema.d.ts`

领域模块独立维护：

- `backend/src/long_invest/modules/watchlists/`：分组、成员、批量结果；
- `backend/src/long_invest/modules/monitor_schedules/`：调度定义、修订和时间校验；
- `backend/src/long_invest/modules/positions/`：当前持仓、历史和资格事件；
- `backend/src/long_invest/modules/monitoring/`：订阅、订阅修订、执行记录和扫描器。

并行任务不得修改 Alembic 主链、主路由、Compose、依赖锁、OpenAPI 文件和生成类型。需要改变公共契约时停止子任务，由主流程串行处理。

### Task 1: Freeze contracts and ORM ownership

**Files:**
- Create: `backend/src/long_invest/modules/watchlists/__init__.py`
- Create: `backend/src/long_invest/modules/watchlists/contracts.py`
- Create: `backend/src/long_invest/modules/watchlists/models.py`
- Create: `backend/src/long_invest/modules/monitor_schedules/__init__.py`
- Create: `backend/src/long_invest/modules/monitor_schedules/contracts.py`
- Create: `backend/src/long_invest/modules/monitor_schedules/models.py`
- Create: `backend/src/long_invest/modules/positions/__init__.py`
- Create: `backend/src/long_invest/modules/positions/contracts.py`
- Create: `backend/src/long_invest/modules/positions/models.py`
- Create: `backend/src/long_invest/modules/monitoring/__init__.py`
- Create: `backend/src/long_invest/modules/monitoring/contracts.py`
- Create: `backend/src/long_invest/modules/monitoring/models.py`
- Create: `backend/tests/modules/watchlists/test_contracts.py`
- Create: `backend/tests/modules/watchlists/test_models.py`
- Create: `backend/tests/modules/monitor_schedules/test_contracts.py`
- Create: `backend/tests/modules/monitor_schedules/test_models.py`
- Create: `backend/tests/modules/positions/test_contracts.py`
- Create: `backend/tests/modules/positions/test_models.py`
- Create: `backend/tests/modules/monitoring/test_contracts.py`
- Create: `backend/tests/modules/monitoring/test_models.py`

- [ ] **Step 1: Write failing contract tests**

Define immutable Pydantic contracts and enums with exact values:

```python
def test_monitoring_enums_are_stable() -> None:
    assert [item.value for item in PositionStatus] == ["HOLDING", "NOT_HOLDING"]
    assert [item.value for item in SubscriptionStatus] == [
        "CONFIGURING", "ENABLED", "PAUSED", "ARCHIVED"
    ]
    assert [item.value for item in OccurrenceStatus] == [
        "PENDING", "CLAIMED", "DISPATCHED", "MISSED", "FAILED"
    ]


def test_schedule_times_are_sorted_and_unique() -> None:
    command = ScheduleDefinition(
        name="午后观察",
        times=(time(14, 30), time(9, 45)),
        reason="initial schedule",
        idempotency_key="schedule-1",
    )
    assert command.times == (time(9, 45), time(14, 30))
```

Also test blank names/reasons, more than 20 times, duplicate times, lunch break, outside market time, position note over 500 characters, invalid hysteresis and empty batch input.

- [ ] **Step 2: Run contract tests and verify RED**

Run:

```text
backend/.venv/Scripts/python.exe -m pytest backend/tests/modules/watchlists/test_contracts.py backend/tests/modules/monitor_schedules/test_contracts.py backend/tests/modules/positions/test_contracts.py backend/tests/modules/monitoring/test_contracts.py -q
```

Expected: collection fails because the four contract modules do not exist.

- [ ] **Step 3: Implement exact public contracts**

Provide these public command/view types:

```python
class WatchlistMutation(StrictContract):
    name: str
    description: str | None = None
    display_order: int = Field(ge=0)
    reason: str
    idempotency_key: str
    expected_version: int | None = Field(default=None, ge=1)


class WatchlistBatchStatus(StrEnum):
    CREATED = "CREATED"
    REUSED = "REUSED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class ScheduleDefinition(StrictContract):
    name: str
    times: tuple[time, ...]
    reason: str
    idempotency_key: str
    expected_version: int | None = Field(default=None, ge=1)


class SetPosition(StrictContract):
    security_id: UUID
    symbol: str
    target: PositionStatus
    note: str | None = Field(default=None, max_length=500)
    source: str
    request_id: str
    idempotency_key: str
    actor_user_id: str
    expected_version: int | None = Field(default=None, ge=1)


class SubscriptionStatus(StrEnum):
    CONFIGURING = "CONFIGURING"
    ENABLED = "ENABLED"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"


class OccurrenceStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    DISPATCHED = "DISPATCHED"
    MISSED = "MISSED"
    FAILED = "FAILED"


class TargetReadinessPort(Protocol):
    async def current_readiness(self, subscription_id: UUID) -> bool: ...


class StrategyReadinessPort(Protocol):
    async def published_version(self, strategy_version_id: UUID) -> bool: ...
```

Use frozen contracts for views and batch results. Convert incoming lists to tuples so callers cannot mutate frozen snapshots.

- [ ] **Step 4: Write failing model metadata tests**

Assert table ownership and database constraints:

```python
def test_one_open_subscription_per_security() -> None:
    index = next(
        item for item in MonitorSubscription.__table__.indexes
        if item.name == "uq_monitor_subscription_open_security"
    )
    assert index.unique is True
    assert "archived_at IS NULL" in str(index.dialect_options["postgresql"]["where"])


def test_position_history_is_append_only_by_shape() -> None:
    table = UserPositionHistory.__table__
    assert table.c.position_version.nullable is False
    assert "uq_user_position_history_security_version" in {
        item.name for item in table.constraints
    }
```

- [ ] **Step 5: Implement ORM models without relationships across modules**

Use UUID primary keys and explicit foreign keys. Required tables and key constraints:

```text
watchlist: owner_user_id, name, description, display_order, version, archived_at
watchlist_item: watchlist_id + security_id composite unique, symbol, source, created_at
monitor_schedule: name, current_revision_id, version, archived_at
monitor_schedule_revision: schedule_id + revision_no unique, times JSONB, timezone, content_hash, reason
user_position: security_id unique, symbol, status, version, latest_history_id, source, updated_at
user_position_history: security_id + position_version unique, before_status, after_status, note, request_id, idempotency_key
monitor_subscription: security_id, symbol, status, current_revision_id, version, archived_at
monitor_subscription_revision: subscription_id + revision_no unique, schedule_id, schedule_revision_id, target_mode, strategy_version_id, parameters JSONB, hysteresis fields, notification_mode, content_hash
schedule_occurrence: occurrence_type + schedule_id + scheduled_at unique, schedule_revision_id, subscription_snapshot JSONB, status, claimed_at, job_id, error_code
```

Do not add ORM relationships between modules. Store external IDs only and resolve them through public applications.

- [ ] **Step 6: Run contract and model tests**

Run all eight test files. Expected: PASS.

- [ ] **Step 7: Run checks and commit**

```text
backend/.venv/Scripts/python.exe -m ruff check backend/src/long_invest/modules/watchlists backend/src/long_invest/modules/monitor_schedules backend/src/long_invest/modules/positions backend/src/long_invest/modules/monitoring backend/tests/modules/watchlists backend/tests/modules/monitor_schedules backend/tests/modules/positions backend/tests/modules/monitoring
git diff --check
git add backend/src/long_invest/modules backend/tests/modules
git commit -m "feat: define monitoring foundation contracts"
```

### Task 2: Create the single 0010 migration

**Files:**
- Modify: `backend/alembic/env.py`
- Create: `backend/alembic/versions/20260716_0010_monitoring_foundations.py`
- Create: `backend/tests/integration/test_monitoring_migration.py`

- [ ] **Step 1: Write migration chain and constraint tests**

Assert `down_revision = "20260716_0009"`, one Alembic head, table creation order, named checks, partial unique subscription index, reverse downgrade order and application-role grants.

- [ ] **Step 2: Run migration tests and verify RED**

Expected: fail because revision `0010` is absent.

- [ ] **Step 3: Import all new models in Alembic metadata**

Add `# noqa: F401` imports to `alembic/env.py`; do not modify any prior migration.

- [ ] **Step 4: Implement 0010**

Create tables in this order:

```text
watchlist
watchlist_item
monitor_schedule
monitor_schedule_revision
user_position
user_position_history
monitor_subscription
monitor_subscription_revision
schedule_occurrence
```

Use `op.f(...)` for convention-expanded check names. Add the partial unique index for one unarchived subscription per security. Downgrade in exact reverse dependency order.

The current-pointer columns create intentional circular references. Create each owner table with a nullable pointer first, create its revision/history table second, then add the named pointer foreign key with `op.create_foreign_key`. Apply the same sequence to schedule/current revision, position/latest history and subscription/current revision; drop those pointer foreign keys before dropping tables.

After the tables are created, revoke `UPDATE` and `DELETE` from the application role on `monitor_schedule_revision`, `user_position_history` and `monitor_subscription_revision`. The owner migration role keeps schema administration rights; normal application code can only append and read immutable records.

- [ ] **Step 5: Verify offline SQL and one head**

```text
cd backend
uv run alembic heads
uv run alembic upgrade 20260716_0009:20260716_0010 --sql
uv run alembic downgrade 20260716_0009:20260716_0010 --sql
```

Expected: one head `20260716_0010`; generated SQL contains no doubled `ck_*_ck_*` names.

- [ ] **Step 6: Verify real PostgreSQL migration round trip**

Use a distinct Compose project and volume:

```text
docker compose -p longinvest-monitoring-migration -f deploy/compose.yaml up -d postgres redis
docker compose -p longinvest-monitoring-migration -f deploy/compose.yaml run --rm migrate
docker compose -p longinvest-monitoring-migration -f deploy/compose.yaml run --rm --no-deps test /app/.venv/bin/alembic downgrade 20260716_0009
docker compose -p longinvest-monitoring-migration -f deploy/compose.yaml run --rm --no-deps test /app/.venv/bin/alembic upgrade 20260716_0010
docker compose -p longinvest-monitoring-migration -f deploy/compose.yaml down -v
```

Check the application role has SELECT/INSERT/UPDATE/DELETE on all nine tables.

- [ ] **Step 7: Commit**

```text
git add backend/alembic backend/tests/integration/test_monitoring_migration.py
git commit -m "feat: persist monitoring foundations"
```

### Task 3: Build watchlists in an isolated worktree

**Parallel batch:** Run Tasks 3, 4 and 5 concurrently in three separate worktrees after Task 2 passes.

**Files:**
- Create: `backend/src/long_invest/modules/watchlists/repository.py`
- Create: `backend/src/long_invest/modules/watchlists/service.py`
- Create: `backend/src/long_invest/modules/watchlists/outbox.py`
- Create: `backend/src/long_invest/modules/watchlists/application.py`
- Create: `backend/src/long_invest/modules/watchlists/api.py`
- Create: `backend/tests/modules/watchlists/test_repository.py`
- Create: `backend/tests/modules/watchlists/test_service.py`
- Create: `backend/tests/modules/watchlists/test_application.py`
- Create: `backend/tests/modules/watchlists/test_api.py`

- [ ] **Step 1: Write failing service tests**

Cover create/replay, version conflict, archive, repeated member, last-membership pause recommendation and per-item batch isolation:

```python
async def test_removing_last_membership_only_recommends_pause() -> None:
    result = await service.remove_item(group_id, security_id, expected_version=3)
    assert result.removed is True
    assert result.pause_recommended is True
    assert subscriptions.calls == []
```

- [ ] **Step 2: Verify RED**

Run watchlist repository/service/application/API tests. Expected: import failure for missing implementations.

- [ ] **Step 3: Implement repository fences**

Provide `get/list/create/find_replay/lock/update_version/archive/add_item/remove_item/count_memberships`. Every mutation includes expected version; a zero-row update raises `WATCHLIST_VERSION_CONFLICT`.

- [ ] **Step 4: Implement service and transaction event adapter**

The service accepts public frozen security identity, never imports `Security` or `MonitorSubscription`. Write `watchlist.updated` with dedupe key `watchlist:{watchlist_id}:{version}:{action}`.

- [ ] **Step 5: Implement application and authenticated API**

Use `SecurityApplication.resolve_identity()` for members. Mutations require verified write identity, reason and `Idempotency-Key`. Batch returns a concrete list of item results and HTTP 200 even when individual entries are rejected.

- [ ] **Step 6: Verify and commit**

```text
backend/.venv/Scripts/python.exe -m pytest backend/tests/modules/watchlists -q
backend/.venv/Scripts/python.exe -m ruff check backend/src/long_invest/modules/watchlists backend/tests/modules/watchlists
git diff --check
git add backend/src/long_invest/modules/watchlists backend/tests/modules/watchlists
git commit -m "feat: manage watchlist groups"
```

### Task 4: Build schedule definitions in an isolated worktree

**Files:**
- Create: `backend/src/long_invest/modules/monitor_schedules/repository.py`
- Create: `backend/src/long_invest/modules/monitor_schedules/service.py`
- Create: `backend/src/long_invest/modules/monitor_schedules/outbox.py`
- Create: `backend/src/long_invest/modules/monitor_schedules/application.py`
- Create: `backend/src/long_invest/modules/monitor_schedules/api.py`
- Create: `backend/tests/modules/monitor_schedules/test_repository.py`
- Create: `backend/tests/modules/monitor_schedules/test_service.py`
- Create: `backend/tests/modules/monitor_schedules/test_application.py`
- Create: `backend/tests/modules/monitor_schedules/test_api.py`

- [ ] **Step 1: Write failing lifecycle tests**

```python
async def test_restore_copies_history_into_a_new_revision() -> None:
    original = await fixture.create(times=("09:45",))
    changed = await fixture.change(times=("14:30",))
    restored = await service.restore(
        changed.schedule_id,
        source_revision_id=original.revision_id,
        expected_version=changed.version,
    )
    assert restored.revision_no == changed.revision_no + 1
    assert restored.id != original.revision_id
    assert restored.times == ("09:45",)
```

Also cover empty schedule, duplicate content replay, archived schedule, version conflict and same-key different-content conflict.

Archive does not silently pause or rewrite existing subscriptions: they keep their frozen revision. Archived schedules are excluded from new subscription configuration and future schedule changes.

- [ ] **Step 2: Verify RED**

Run schedule tests. Expected: missing repository/service/application/API.

- [ ] **Step 3: Implement immutable revisions and atomic current pointer**

Lock the schedule row, compute the next revision, insert immutable revision, update current pointer and version, append audit and `monitor_schedule.changed` within one transaction.

Expose `MonitorScheduleApplication.current_revision(schedule_id)` returning a frozen revision view. Monitoring consumers use this method and never import schedule ORM or repository types.

- [ ] **Step 4: Implement concrete API**

Provide list/create/get/patch/archive, versions and restore endpoints. Return times as `HH:MM` strings in ascending order. Missing required idempotency or expected version returns a stable 422; stale version returns 409.

- [ ] **Step 5: Verify and commit**

```text
backend/.venv/Scripts/python.exe -m pytest backend/tests/modules/monitor_schedules -q
backend/.venv/Scripts/python.exe -m ruff check backend/src/long_invest/modules/monitor_schedules backend/tests/modules/monitor_schedules
git diff --check
git add backend/src/long_invest/modules/monitor_schedules backend/tests/modules/monitor_schedules
git commit -m "feat: version monitor schedules"
```

### Task 5: Build positions in an isolated worktree

**Files:**
- Create: `backend/src/long_invest/modules/positions/repository.py`
- Create: `backend/src/long_invest/modules/positions/service.py`
- Create: `backend/src/long_invest/modules/positions/outbox.py`
- Create: `backend/src/long_invest/modules/positions/application.py`
- Create: `backend/src/long_invest/modules/positions/api.py`
- Create: `backend/tests/modules/positions/test_repository.py`
- Create: `backend/tests/modules/positions/test_service.py`
- Create: `backend/tests/modules/positions/test_application.py`
- Create: `backend/tests/modules/positions/test_api.py`
- Create: `backend/tests/integration/test_position_transaction.py`

- [ ] **Step 1: Write failing state and idempotency tests**

```python
async def test_same_position_is_idempotent_without_history_or_business_event() -> None:
    first = await service.set(command(PositionStatus.HOLDING))
    replay = await service.set(command(PositionStatus.HOLDING))
    assert replay.code == "POSITION_UNCHANGED"
    assert replay.version == first.version
    assert history.count == 1
    assert events.business_event_count == 3  # changed, became_holding, review request
```

Cover default NOT_HOLDING, optimistic conflict, HOLDING events, NOT_HOLDING cancellation event, note length, append-only history and concurrent changes.

- [ ] **Step 2: Verify RED**

Run position tests. Expected: missing implementations.

- [ ] **Step 3: Implement locked current state and append-only history**

When no current row exists, serialize by a transaction advisory lock on security ID. For a real transition, create exactly one history row, update `latest_history_id`, increment version and write all required events. Never accept a client-provided effective timestamp.

- [ ] **Step 4: Implement public event payloads**

Use dedupe keys containing security and position version:

```text
position:{security_id}:{version}:changed
position:{security_id}:{version}:became-holding
position:{security_id}:{version}:high-review
position:{security_id}:{version}:became-not-holding
position:{security_id}:{version}:cancel-high-notifications
```

- [ ] **Step 5: Implement API and transaction rollback test**

Provide list, get, history, hold, clear and batch. Inject a failing audit/outbox writer and prove current state and history both roll back. Batch uses one transaction per item so a rejected symbol does not roll back successful symbols.

- [ ] **Step 6: Verify and commit**

```text
backend/.venv/Scripts/python.exe -m pytest backend/tests/modules/positions backend/tests/integration/test_position_transaction.py -q
backend/.venv/Scripts/python.exe -m ruff check backend/src/long_invest/modules/positions backend/tests/modules/positions backend/tests/integration/test_position_transaction.py
git diff --check
git add backend/src/long_invest/modules/positions backend/tests/modules/positions backend/tests/integration/test_position_transaction.py
git commit -m "feat: track current positions and history"
```

### Task 6: Integrate monitor subscriptions serially

**Files:**
- Create: `backend/src/long_invest/modules/monitoring/repository.py`
- Create: `backend/src/long_invest/modules/monitoring/service.py`
- Create: `backend/src/long_invest/modules/monitoring/outbox.py`
- Create: `backend/src/long_invest/modules/monitoring/application.py`
- Create: `backend/src/long_invest/modules/monitoring/api.py`
- Create: `backend/tests/modules/monitoring/test_repository.py`
- Create: `backend/tests/modules/monitoring/test_service.py`
- Create: `backend/tests/modules/monitoring/test_application.py`
- Create: `backend/tests/modules/monitoring/test_api.py`
- Create: `backend/tests/integration/test_subscription_pause_fence.py`

- [ ] **Step 1: Write failing lifecycle and public-integration tests**

Test create/reuse, one open subscription per security, CONFIGURING creation, schedule revision freeze, enable rejection without target, pause, archive precondition, restore-to-PAUSED, version conflict and pause fence.

```python
async def test_new_subscription_is_configuring_until_target_exists() -> None:
    result = await application.create(symbol="600000.SH", idempotency_key="sub-1")
    assert result.status is SubscriptionStatus.CONFIGURING
    with pytest.raises(AppError) as caught:
        await application.enable(result.id, expected_version=result.version)
    assert caught.value.code == "MONITOR_SUBSCRIPTION_NOT_READY"
```

- [ ] **Step 2: Verify RED**

Expected: missing monitoring implementations.

- [ ] **Step 3: Implement repository and lifecycle service**

Use the partial unique index as final race protection. Every lifecycle transition includes expected status and version. Archive only from PAUSED; restore always transitions to PAUSED.

- [ ] **Step 4: Implement public integration ports**

Application may call only:

```text
SecurityApplication.resolve_identity(symbol)
MonitorScheduleApplication.current_revision(schedule_id)
TargetReadinessPort.current_readiness(subscription_id)
StrategyReadinessPort.published_version(strategy_version_id)
```

For this batch inject readiness ports that return unavailable. This makes `enable()` reliably return `MONITOR_SUBSCRIPTION_NOT_READY` until the next target/strategy plan supplies real adapters.

- [ ] **Step 5: Implement audit, events and API**

Write `monitor_subscription.created/enabled/disabled/changed/archived` through the same transaction. Provide list/create/patch/enable/disable/archive/restore routes. `check-now` and `diagnose` return `MONITOR_CAPABILITY_NOT_READY` with HTTP 409 until target/signal integration.

- [ ] **Step 6: Verify pause fence**

In the integration test, freeze an enabled snapshot, pause the subscription, then attempt final eligibility with the old version. Assert it returns `SUPERSEDED` and writes no signal/notification event.

- [ ] **Step 7: Verify and commit**

```text
backend/.venv/Scripts/python.exe -m pytest backend/tests/modules/monitoring backend/tests/integration/test_subscription_pause_fence.py -q
backend/.venv/Scripts/python.exe -m ruff check backend/src/long_invest/modules/monitoring backend/tests/modules/monitoring backend/tests/integration/test_subscription_pause_fence.py
git diff --check
git add backend/src/long_invest/modules/monitoring backend/tests/modules/monitoring backend/tests/integration/test_subscription_pause_fence.py
git commit -m "feat: manage monitor subscriptions"
```

### Task 7: Add schedule occurrences and the ten-second scanner

**Files:**
- Create: `backend/src/long_invest/modules/monitoring/scheduler.py`
- Create: `backend/src/long_invest/entrypoints/monitor_scheduler.py`
- Modify: `deploy/compose.yaml`
- Create: `backend/tests/modules/monitoring/test_scheduler.py`
- Create: `backend/tests/integration/test_monitor_scheduler.py`
- Modify: `backend/tests/integration/test_worker_queue_isolation.py`

- [ ] **Step 1: Write failing pure scanner tests**

Cover trading-day gate, 60-second claim window, MISSED records, duplicate scans, merged subscriptions, frozen schedule/subscription versions and overlapping occurrences.

```python
async def test_late_occurrence_is_missed_and_never_dispatched() -> None:
    result = await scanner.scan(now=scheduled_at + timedelta(seconds=61))
    assert result.missed == 1
    assert jobs.submissions == []
```

- [ ] **Step 2: Verify RED**

Expected: missing scheduler module and Compose service.

- [ ] **Step 3: Implement occurrence claim transaction**

Use public calendar and subscription snapshot ports. Inside one database transaction, insert the unique occurrence, submit one `REALTIME_QUOTE_CYCLE` job to `realtime-quotes`, and append the occurrence event. A unique conflict means another scanner owns the occurrence and is a successful no-op.

- [ ] **Step 4: Implement resilient loop**

`monitor_scheduler.py` runs one scan immediately, then waits until ten seconds have elapsed using condition-based waiting. Catch and log one scan failure, continue the next scan, and close database resources on shutdown. Do not enqueue missed occurrences after restart.

- [ ] **Step 5: Add isolated Compose service**

Add `monitor-scheduler` using the backend runtime image, read-only filesystem, no-new-privileges, memory limit 128 MB, application log volume and no public port.

- [ ] **Step 6: Verify real PostgreSQL duplicate scanners**

Start two scanner calls for the same planned UTC time. Assert exactly one occurrence and one job exist. Restart the scanner after the 60-second window and assert the occurrence is MISSED rather than dispatched.

- [ ] **Step 7: Verify and commit**

```text
backend/.venv/Scripts/python.exe -m pytest backend/tests/modules/monitoring/test_scheduler.py backend/tests/integration/test_monitor_scheduler.py backend/tests/integration/test_worker_queue_isolation.py -q
git diff --check
git add backend/src/long_invest/modules/monitoring/scheduler.py backend/src/long_invest/entrypoints/monitor_scheduler.py deploy/compose.yaml backend/tests
git commit -m "feat: schedule monitor quote cycles"
```

### Task 8: Serial route, OpenAPI and container integration

**Files:**
- Modify: `backend/src/long_invest/bootstrap/app.py`
- Modify: `backend/openapi.json`
- Modify: `frontend/src/shared/api/generated/schema.d.ts`
- Create: `backend/tests/integration/test_monitoring_stage3.py`

- [ ] **Step 1: Write failing main-app and schema tests**

Assert every route from the design is present, all success responses reference concrete schemas, write operations expose required `Idempotency-Key`, and operation IDs are unique.

- [ ] **Step 2: Register routers serially**

Include only the four public routers in `bootstrap/app.py`. Do not import repository or ORM modules from the bootstrap layer.

- [ ] **Step 3: Export and regenerate contracts**

```text
cd backend
uv run python -m long_invest.entrypoints.export_openapi
cd ../frontend
npm run generate:api
```

Assert saved OpenAPI equals runtime OpenAPI.

- [ ] **Step 4: Run backend verification**

```text
cd backend
uv run pytest -q
uv run ruff check .
uv run python -m compileall -q src
uv run alembic heads
git diff --check
```

Expected: all tests pass and the only head is `20260716_0010`.

- [ ] **Step 5: Run frontend verification**

```text
cd frontend
npm test -- --run
npm run lint
npm run typecheck
npm run build
```

Expected: all commands pass. Generated TypeScript contains concrete watchlist, schedule, position, subscription and occurrence responses.

- [ ] **Step 6: Run isolated full container acceptance**

Use a new Compose project and volume, migrate from empty database, run the full test profile, then remove it. Verify application role access and real concurrent tests.

- [ ] **Step 7: Deploy and verify production Compose**

```text
git push server main
docker compose -f deploy/compose.yaml up -d --build
```

Verify migration `20260716_0010`, `/health/ready`, public frontend 200, all new business routes return 401 without login, `monitor-scheduler` is running with restart count zero, and no new public ports exist.

- [ ] **Step 8: Commit generated integration artifacts**

```text
git add backend/src/long_invest/bootstrap/app.py backend/openapi.json frontend/src/shared/api/generated/schema.d.ts backend/tests/integration/test_monitoring_stage3.py
git commit -m "feat: integrate monitoring foundations"
```

## Completion gate

The plan is complete only when:

- all Task 1 and Task 2 base checks pass before parallel work starts;
- Tasks 3, 4 and 5 are reviewed independently and cherry-picked one at a time;
- subscription and occurrence tasks use only public contracts;
- migration upgrade, downgrade and re-upgrade succeed on isolated PostgreSQL;
- audit/outbox failure proves rollback for every high-risk mutation;
- full backend, frontend and Docker acceptance pass after all modules are integrated;
- local `main` and server `main` point to the same clean commit.
