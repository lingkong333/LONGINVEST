# Stage 3 Manual Targets and Signals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成手工四档目标、历史恢复、目标激活重评、五区间信号状态机、滞回、乱序保护和持仓通知资格，形成阶段 3 监控闭环。

**Architecture:** `targets` 和 `signals` 是两个独立领域模块，通过不可变公开契约读取监控订阅、行情、持仓和通知能力。主流程串行冻结公共契约、迁移、任务、路由和 Compose；目标持久化与信号纯算法可在底座完成后独立施工，信号事务最后接入目标快照和通知发布端口。

**Tech Stack:** Python 3.12+、FastAPI、Pydantic、Decimal、SQLAlchemy 2、Alembic、PostgreSQL 16、Redis/RQ、pytest、Ruff、OpenAPI TypeScript、Docker Compose。

---

## 文件结构与所有权

主流程串行维护：

- `backend/alembic/env.py`
- `backend/alembic/versions/20260717_0011_targets_signals.py`
- `backend/src/long_invest/bootstrap/app.py`
- `backend/src/long_invest/bootstrap/jobs.py`
- `backend/src/long_invest/entrypoints/job_worker.py`
- `deploy/compose.yaml`
- `backend/openapi.json`
- `frontend/src/shared/api/generated/schema.d.ts`

目标模块拥有：

- `backend/src/long_invest/modules/targets/contracts.py`
- `backend/src/long_invest/modules/targets/models.py`
- `backend/src/long_invest/modules/targets/repository.py`
- `backend/src/long_invest/modules/targets/service.py`
- `backend/src/long_invest/modules/targets/outbox.py`
- `backend/src/long_invest/modules/targets/application.py`
- `backend/src/long_invest/modules/targets/api.py`

信号模块拥有：

- `backend/src/long_invest/modules/signals/contracts.py`
- `backend/src/long_invest/modules/signals/models.py`
- `backend/src/long_invest/modules/signals/state_machine.py`
- `backend/src/long_invest/modules/signals/repository.py`
- `backend/src/long_invest/modules/signals/service.py`
- `backend/src/long_invest/modules/signals/integrations.py`
- `backend/src/long_invest/modules/signals/application.py`
- `backend/src/long_invest/modules/signals/api.py`

子任务不得修改主路由、迁移主链、Compose、任务注册、OpenAPI 和生成类型。任何公共契约变化先返回主流程处理。

## 验证节奏

- Tasks 1-8 只运行当前文件和直接关联模块的最小测试。
- 真实 PostgreSQL 只用于迁移、并发、事务回滚和恢复场景。
- Task 9 才运行后端、前端、数据库和容器全量验收。

### Task 1: Freeze public contracts and ORM ownership

**Files:**
- Create: `backend/src/long_invest/modules/targets/__init__.py`
- Create: `backend/src/long_invest/modules/targets/contracts.py`
- Create: `backend/src/long_invest/modules/targets/models.py`
- Create: `backend/src/long_invest/modules/signals/__init__.py`
- Create: `backend/src/long_invest/modules/signals/contracts.py`
- Create: `backend/src/long_invest/modules/signals/models.py`
- Create: `backend/tests/modules/targets/test_contracts.py`
- Create: `backend/tests/modules/targets/test_models.py`
- Create: `backend/tests/modules/signals/test_contracts.py`
- Create: `backend/tests/modules/signals/test_models.py`

- [ ] **Step 1: Write failing target contract tests**

```python
from decimal import Decimal

import pytest
from pydantic import ValidationError

from long_invest.modules.targets.contracts import TargetSource, TargetStatus, TargetValues


def test_target_enums_are_stable() -> None:
    assert [item.value for item in TargetSource] == ["MANUAL", "RESTORED"]
    assert [item.value for item in TargetStatus] == [
        "READY", "STALE", "CALCULATING", "REVIEW_REQUIRED",
        "ACTIVATING", "FAILED", "MISSING",
    ]


def test_target_values_quantize_and_require_strict_order() -> None:
    values = TargetValues(
        low_strong="8.001", low_watch="9.004", high_watch="12.006", high_strong="13.009"
    )
    assert values.low_strong == Decimal("8.00")
    assert values.high_strong == Decimal("13.01")
    with pytest.raises(ValidationError):
        TargetValues(low_strong="9", low_watch="9", high_watch="12", high_strong="13")
```

- [ ] **Step 2: Write failing signal contract tests**

```python
from long_invest.modules.signals.contracts import (
    EvaluationReason, EvaluationResult, SignalZone,
)


def test_signal_enums_are_stable() -> None:
    assert [item.value for item in SignalZone] == [
        "UNKNOWN", "STRONG_LOW", "LOW", "NORMAL", "HIGH", "STRONG_HIGH"
    ]
    assert [item.value for item in EvaluationResult] == [
        "APPLIED", "UNCHANGED", "SKIPPED", "SUPERSEDED"
    ]
    assert "TARGET_ACTIVATED" in {item.value for item in EvaluationReason}
```

- [ ] **Step 3: Run contract tests and verify RED**

Run:

```text
cd backend
uv run pytest -q tests/modules/targets/test_contracts.py tests/modules/signals/test_contracts.py
```

Expected: collection fails because `targets` and `signals` do not exist.

- [ ] **Step 4: Implement immutable public contracts**

`targets/contracts.py` must define `StrictContract`, `TargetSource`, `TargetStatus`, `TargetValues`, `ManualTargetCommand`, `RestoreTargetCommand`, `TargetRevisionView`, `TargetBindingView`, `TargetSnapshot`, `TargetMutationResult` and `TargetSnapshotPort`.

```python
CENT = Decimal("0.01")


class TargetValues(StrictContract):
    low_strong: Decimal
    low_watch: Decimal
    high_watch: Decimal
    high_strong: Decimal

    @model_validator(mode="after")
    def validate_values(self):
        values = tuple(value.quantize(CENT, rounding=ROUND_HALF_UP) for value in (
            self.low_strong, self.low_watch, self.high_watch, self.high_strong
        ))
        if any(not value.is_finite() or value <= 0 for value in values):
            raise ValueError("target values must be finite and positive")
        if not values[0] < values[1] < values[2] < values[3]:
            raise ValueError("target values must be strictly increasing")
        object.__setattr__(self, "low_strong", values[0])
        object.__setattr__(self, "low_watch", values[1])
        object.__setattr__(self, "high_watch", values[2])
        object.__setattr__(self, "high_strong", values[3])
        return self
```

`signals/contracts.py` must define `SignalZone`, `EvaluationReason`, `EvaluationResult`, `NotificationClass`, `SignalInput`, `SignalStateView`, `SignalEvaluationView`, `SignalEventView`, `EvaluationOutcome`, and public snapshot ports for subscription, position and target reads.

- [ ] **Step 5: Implement ORM ownership without repositories**

`targets/models.py` contains only `TargetRevision` and `SubscriptionTargetBinding`. `signals/models.py` contains only `SignalState`, `SignalEvaluation` and `SignalEvent`. Add check constraints for enum values, positive versions and target ordering; add unique constraints for binding per subscription, state per subscription and evaluation idempotency.

```python
CheckConstraint(
    "low_strong > 0 AND low_strong < low_watch "
    "AND low_watch < high_watch AND high_watch < high_strong",
    name="target_values_ordered",
)
```

- [ ] **Step 6: Run contract/model tests and verify GREEN**

```text
cd backend
uv run pytest -q tests/modules/targets/test_contracts.py tests/modules/targets/test_models.py tests/modules/signals/test_contracts.py tests/modules/signals/test_models.py
uv run ruff check src/long_invest/modules/targets src/long_invest/modules/signals tests/modules/targets tests/modules/signals
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit contracts and models**

```text
git add backend/src/long_invest/modules/targets backend/src/long_invest/modules/signals backend/tests/modules/targets backend/tests/modules/signals
git commit -m "feat: freeze target and signal contracts"
```

### Task 2: Create the single stage migration

**Files:**
- Modify: `backend/alembic/env.py`
- Create: `backend/alembic/versions/20260717_0011_targets_signals.py`
- Create: `backend/tests/integration/test_targets_signals_migration.py`

- [ ] **Step 1: Write failing migration metadata test**

```python
def test_targets_signals_migration_is_the_only_head() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == ["20260717_0011"]
```

Add a PostgreSQL-gated test that upgrades an empty database, verifies all five tables, constraints, indexes, application-role CRUD permissions and immutable-table update/delete denial, then downgrades to `20260716_0010` and upgrades again.

- [ ] **Step 2: Run migration test and verify RED**

```text
cd backend
uv run pytest -q tests/integration/test_targets_signals_migration.py
```

Expected: fails because revision `20260717_0011` does not exist.

- [ ] **Step 3: Implement revision `20260717_0011`**

Use `down_revision = "20260716_0010"`. Create in dependency order:

```text
target_revision
subscription_target_binding
signal_state
signal_evaluation
signal_event
```

Grant the application role `SELECT/INSERT/UPDATE/DELETE` only where business mutations require it. Install database triggers that reject `UPDATE/DELETE` on `target_revision`, `signal_evaluation` and `signal_event`. Do not create `target_calculation_run` or `target_review` in this revision.

- [ ] **Step 4: Run the local migration metadata gate**

```text
cd backend
uv run pytest -q tests/integration/test_targets_signals_migration.py -k "only_head or model"
uv run alembic heads
```

Expected: one head, `20260717_0011`.

- [ ] **Step 5: Run the isolated PostgreSQL migration gate**

Use a fresh Compose project and only the migration test:

```text
docker compose -p longinvest-stage3-ts-migration -f deploy/compose.yaml --profile test run --rm --build test pytest -q tests/integration/test_targets_signals_migration.py
docker compose -p longinvest-stage3-ts-migration -f deploy/compose.yaml --profile test down -v
```

Expected: migration upgrade, downgrade and re-upgrade pass.

- [ ] **Step 6: Commit migration**

```text
git add backend/alembic backend/tests/integration/test_targets_signals_migration.py
git commit -m "feat: add target and signal storage"
```

### Task 3: Implement manual targets and historical restore

**Files:**
- Create: `backend/src/long_invest/modules/targets/repository.py`
- Create: `backend/src/long_invest/modules/targets/service.py`
- Create: `backend/src/long_invest/modules/targets/outbox.py`
- Create: `backend/src/long_invest/modules/targets/application.py`
- Create: `backend/tests/modules/targets/test_repository.py`
- Create: `backend/tests/modules/targets/test_service.py`
- Create: `backend/tests/modules/targets/test_application.py`
- Create: `backend/tests/integration/test_target_transaction.py`

- [ ] **Step 1: Write failing service tests for manual activation**

```python
@pytest.mark.anyio
async def test_manual_target_creates_revision_binding_audit_and_reevaluation() -> None:
    result = await service.set_manual(command())
    assert result.binding.status is TargetStatus.READY
    assert result.revision.source is TargetSource.MANUAL
    assert events.types == ["target.activated", "signal.reevaluation_requested"]
    assert audit.actions == ["target.manual_activated"]


@pytest.mark.anyio
async def test_large_manual_change_requires_second_confirmation() -> None:
    repository.current = existing(values=("8", "9", "12", "13"))
    with pytest.raises(AppError) as error:
        await service.set_manual(command(values=("4", "5", "14", "15")))
    assert error.value.code == "TARGET_CONFIRMATION_REQUIRED"
```

Also cover same-content replay, different-content idempotency conflict, expected binding version conflict, archived subscription, and strategy-to-manual mode switch confirmation.

- [ ] **Step 2: Run target service tests and verify RED**

```text
cd backend
uv run pytest -q tests/modules/targets/test_service.py tests/modules/targets/test_application.py
```

Expected: fails because target service and application do not exist.

- [ ] **Step 3: Implement repository locking and immutable writes**

Repository methods:

```python
async def lock_binding(self, subscription_id: UUID) -> SubscriptionTargetBinding | None
async def create_binding(self, subscription_id: UUID) -> SubscriptionTargetBinding
async def find_revision_by_idempotency(self, subscription_id: UUID, key: str) -> TargetRevision | None
async def get_revision(self, revision_id: UUID) -> TargetRevision | None
async def list_revisions(self, subscription_id: UUID) -> tuple[TargetRevision, ...]
async def persist_revision(self, revision: TargetRevision) -> None
async def flush(self) -> None
```

Use `SELECT ... FOR UPDATE` for binding mutations. Repository returns module models only and never commits.

- [ ] **Step 4: Implement target service transaction logic**

Calculate large changes with:

```python
def relative_change(before: Decimal, after: Decimal) -> Decimal:
    return abs(after - before) / max(abs(before), Decimal("0.01"))
```

For manual activation, lock the binding, validate expected version, resolve idempotency, request a transaction-bound monitoring mode switch when needed, create the immutable revision, update the pointer, append audit and both outbox events, then flush. Never commit inside the service.

- [ ] **Step 5: Implement historical restore**

Restore copies values into a new `TargetRevision(source="RESTORED", restored_from_revision_id=...)`, requires expected binding version and confirmation, and never repoints directly to the historical row.

- [ ] **Step 6: Implement application transaction boundary**

`TargetApplication` accepts an injected `TargetSubscriptionPort`. Module tests use a transaction-bound fake; the production monitoring adapter is supplied by Task 6 before router registration. The application binds repository, service, audit, outbox and the injected port to one database transaction for writes. Translate database or timeout failures to `TARGET_BACKEND_UNAVAILABLE`.

- [ ] **Step 7: Prove transaction rollback with PostgreSQL**

`test_target_transaction.py` must inject audit failure, target outbox failure and monitoring mode-switch failure separately. After each failure assert no target revision, binding change, subscription revision or audit/outbox residue exists.

Run only:

```text
cd backend
uv run pytest -q tests/modules/targets tests/integration/test_target_transaction.py
uv run ruff check src/long_invest/modules/targets tests/modules/targets tests/integration/test_target_transaction.py
```

Expected: selected tests pass; PostgreSQL gate may skip locally and runs in Task 9.

- [ ] **Step 8: Commit target domain**

```text
git add backend/src/long_invest/modules/targets backend/tests/modules/targets backend/tests/integration/test_target_transaction.py
git commit -m "feat: manage manual target versions"
```

### Task 4: Implement target HTTP API

**Files:**
- Create: `backend/src/long_invest/modules/targets/api.py`
- Create: `backend/tests/modules/targets/test_api.py`

- [ ] **Step 1: Write failing API tests**

Test authenticated reads, verified writes, concrete response schemas, required `Idempotency-Key`, strict bodies, explicit `confirm`, large-change confirmation, strategy-mode switch confirmation and capability errors.

```python
def test_target_openapi_has_concrete_models_and_required_header() -> None:
    schema = app().openapi()["paths"]
    manual = schema["/api/v1/targets/{subscription_id}/manual"]["post"]
    assert "$ref" in manual["responses"]["200"]["content"]["application/json"]["schema"]
    header = next(p for p in manual["parameters"] if p["name"] == "Idempotency-Key")
    assert header["required"] is True
```

- [ ] **Step 2: Run API tests and verify RED**

```text
cd backend
uv run pytest -q tests/modules/targets/test_api.py
```

Expected: fails because `targets.api` does not exist.

- [ ] **Step 3: Implement target routes**

Implement usable routes:

```text
GET  /api/v1/targets
GET  /api/v1/targets/{subscription_id}
GET  /api/v1/targets/{subscription_id}/history
POST /api/v1/targets/{subscription_id}/manual
POST /api/v1/targets/{subscription_id}/restore
```

Expose calculate, retry, batch calculation and review routes with concrete response models, authentication and `TARGET_CAPABILITY_NOT_READY` status 409. Do not register the router in `bootstrap/app.py` yet.

- [ ] **Step 4: Run the target module gate**

```text
cd backend
uv run pytest -q tests/modules/targets
uv run ruff check src/long_invest/modules/targets tests/modules/targets
```

Expected: target tests pass.

- [ ] **Step 5: Commit target API**

```text
git add backend/src/long_invest/modules/targets/api.py backend/tests/modules/targets/test_api.py
git commit -m "feat: expose manual target API"
```

### Task 5: Implement the pure signal state machine

**Files:**
- Create: `backend/src/long_invest/modules/signals/state_machine.py`
- Create: `backend/tests/modules/signals/test_state_machine.py`

- [ ] **Step 1: Write exhaustive boundary tests**

```python
@pytest.mark.parametrize(
    ("price", "zone"),
    [
        ("8.00", "STRONG_LOW"), ("8.01", "LOW"), ("9.00", "LOW"),
        ("9.01", "NORMAL"), ("11.99", "NORMAL"), ("12.00", "HIGH"),
        ("12.99", "HIGH"), ("13.00", "STRONG_HIGH"),
    ],
)
def test_base_zone_boundaries(price, zone, values) -> None:
    assert base_zone(Decimal(price), values).value == zone
```

Add tests for all four exit buffers, cross-multiple-zone jumps, `TARGET_ACTIVATED` bypassing old hysteresis, `UNKNOWN -> NORMAL` event suppression and invalid non-positive/non-finite price.

- [ ] **Step 2: Run state-machine tests and verify RED**

```text
cd backend
uv run pytest -q tests/modules/signals/test_state_machine.py
```

Expected: fails because `signals.state_machine` does not exist.

- [ ] **Step 3: Implement deterministic Decimal-only functions**

Public functions:

```python
def base_zone(price: Decimal, targets: TargetValues) -> SignalZone
def hysteresis_buffer(target: Decimal, ratio: Decimal, minimum: Decimal) -> Decimal
def next_zone(current: SignalZone, signal_input: SignalInput) -> SignalZone
def notification_class(before: SignalZone, after: SignalZone) -> NotificationClass | None
def should_create_event(before: SignalZone, after: SignalZone) -> bool
```

No database, clock, network, logging or module service imports are permitted in this file.

- [ ] **Step 4: Run pure algorithm gate**

```text
cd backend
uv run pytest -q tests/modules/signals/test_state_machine.py
uv run ruff check src/long_invest/modules/signals/state_machine.py tests/modules/signals/test_state_machine.py
```

Expected: all state-machine tests pass.

- [ ] **Step 5: Commit state machine**

```text
git add backend/src/long_invest/modules/signals/state_machine.py backend/tests/modules/signals/test_state_machine.py
git commit -m "feat: implement signal hysteresis state machine"
```

### Task 6: Add transaction-bound cross-module ports

**Files:**
- Modify: `backend/src/long_invest/modules/monitoring/contracts.py`
- Modify: `backend/src/long_invest/modules/monitoring/application.py`
- Modify: `backend/src/long_invest/modules/positions/contracts.py`
- Modify: `backend/src/long_invest/modules/positions/application.py`
- Modify: `backend/src/long_invest/modules/notifications/service.py`
- Create: `backend/src/long_invest/modules/signals/integrations.py`
- Modify: `backend/tests/modules/monitoring/test_application.py`
- Modify: `backend/tests/modules/positions/test_application.py`
- Create: `backend/tests/modules/signals/test_integrations.py`

- [ ] **Step 1: Write failing public-port tests**

Prove the monitoring port can lock/read a subscription snapshot and switch one subscription to MANUAL inside a caller-owned transaction. Prove the position port reads a versioned immutable snapshot. Prove the notification port can publish using a caller-provided session without committing.

```python
@pytest.mark.anyio
async def test_transaction_bound_notification_publish_uses_caller_session(session) -> None:
    publisher = TransactionalNotificationPublisher(session)
    await publisher.publish(signal_notification())
    await session.rollback()
    assert await stored_notifications(session_factory) == 0
```

- [ ] **Step 2: Run integration-port tests and verify RED**

```text
cd backend
uv run pytest -q tests/modules/monitoring/test_application.py tests/modules/positions/test_application.py tests/modules/signals/test_integrations.py
```

Expected: new public methods and signal adapters are missing.

- [ ] **Step 3: Implement minimal public ports**

Add immutable contracts:

```python
class SubscriptionSignalSnapshot(StrictContract):
    subscription_id: UUID
    security_id: UUID
    symbol: str
    status: SubscriptionStatus
    version: int
    revision_id: UUID
    target_mode: str
    hysteresis_ratio: Decimal
    hysteresis_min: Decimal
    notification_mode: str


class PositionSnapshot(StrictContract):
    security_id: UUID
    status: PositionStatus
    version: int
```

Application methods accepting a caller session must not open, commit or close transactions. Keep existing HTTP-facing application methods unchanged.

- [ ] **Step 4: Implement signal adapters**

Add `transactional_notification_service(session)` in `notifications/service.py`; that public factory constructs its own repository inside the notifications module. `signals/integrations.py` calls only this factory and public monitoring/position applications. The signal service receives only a `SignalNotificationPort` and never imports `NotificationRepository`.

- [ ] **Step 5: Run the cross-module gate**

```text
cd backend
uv run pytest -q tests/modules/monitoring/test_application.py tests/modules/positions/test_application.py tests/modules/signals/test_integrations.py
uv run ruff check src/long_invest/modules/monitoring src/long_invest/modules/positions src/long_invest/modules/signals/integrations.py
```

Expected: selected tests pass and caller rollback removes all cross-module writes.

- [ ] **Step 6: Commit shared ports**

```text
git add backend/src/long_invest/modules/monitoring backend/src/long_invest/modules/positions backend/src/long_invest/modules/notifications/service.py backend/src/long_invest/modules/signals/integrations.py backend/tests/modules
git commit -m "feat: expose target signal integration ports"
```

### Task 7: Implement signal persistence, atomic evaluation and concurrency

**Files:**
- Create: `backend/src/long_invest/modules/signals/repository.py`
- Create: `backend/src/long_invest/modules/signals/service.py`
- Create: `backend/src/long_invest/modules/signals/application.py`
- Create: `backend/tests/modules/signals/test_repository.py`
- Create: `backend/tests/modules/signals/test_service.py`
- Create: `backend/tests/modules/signals/test_application.py`
- Create: `backend/tests/integration/test_signal_transaction.py`

- [ ] **Step 1: Write failing atomic evaluation tests**

```python
@pytest.mark.anyio
async def test_unknown_to_normal_is_silent_baseline() -> None:
    result = await service.evaluate(valid_input(price="10.00"))
    assert result.state.zone is SignalZone.NORMAL
    assert result.evaluation.result is EvaluationResult.APPLIED
    assert result.event is None
    assert notifications.items == []


@pytest.mark.anyio
async def test_high_without_position_persists_event_but_suppresses_notification() -> None:
    result = await service.evaluate(valid_input(price="12.50"), position=not_holding())
    assert result.event is not None
    assert result.event.notification_eligible is False
    assert result.event.suppression_reason == "NOT_HOLDING"
```

Also cover unchanged comparisons, low without holding, direct high-to-low transition, disabled subscription, missing target, ineligible quote, stale-but-valid target, old quote, old target, old subscription and same/different idempotency replay.

- [ ] **Step 2: Run signal service tests and verify RED**

```text
cd backend
uv run pytest -q tests/modules/signals/test_service.py tests/modules/signals/test_application.py
```

Expected: signal repository/service/application do not exist.

- [ ] **Step 3: Implement repository locks and stable queries**

Repository methods lock state with `FOR UPDATE`, initialize UNKNOWN once, find evaluations by idempotency, persist evaluation/event, update state, and provide paginated reads ordered by `created_at DESC, id DESC`. Repository never commits.

- [ ] **Step 4: Implement ordered evaluation service**

Evaluation order is fixed:

```text
idempotency replay
lock/init state
subscription enabled and version current
target available and version current
quote eligible and newer than current state
position snapshot
write evaluation
write event/update state when changed
publish or suppress notification
flush
```

Skipped and superseded inputs always write an evaluation and never update state. Notification failure must raise so the caller transaction rolls back evaluation, event and state together.

- [ ] **Step 5: Implement application transaction boundary**

`SignalApplication.evaluate()` opens one transaction, binds repository plus subscription/target/position/notification ports to the same session and invokes the service. Read methods use read-only sessions. Database/timeout errors map to `SIGNAL_BACKEND_UNAVAILABLE`.

- [ ] **Step 6: Prove PostgreSQL concurrency and rollback**

`test_signal_transaction.py` starts two evaluations for the same UNKNOWN state and same transition. Assert one current state version, one signal event and one notification idempotency record. Separately inject event, notification and outbox failure and assert the entire evaluation transaction rolls back.

Run minimal gate:

```text
cd backend
uv run pytest -q tests/modules/signals tests/integration/test_signal_transaction.py
uv run ruff check src/long_invest/modules/signals tests/modules/signals tests/integration/test_signal_transaction.py
```

Expected: module tests pass; real PostgreSQL gate runs with its explicit environment flag in Task 9.

- [ ] **Step 7: Commit signal domain**

```text
git add backend/src/long_invest/modules/signals backend/tests/modules/signals backend/tests/integration/test_signal_transaction.py
git commit -m "feat: persist atomic signal evaluations"
```

### Task 8: Add signal API, workers and event wiring

**Files:**
- Create: `backend/src/long_invest/modules/signals/api.py`
- Create: `backend/src/long_invest/modules/signals/projector.py`
- Modify: `backend/src/long_invest/bootstrap/jobs.py`
- Modify: `backend/src/long_invest/entrypoints/job_worker.py`
- Create: `backend/src/long_invest/entrypoints/signal_projector.py`
- Modify: `deploy/compose.yaml`
- Create: `backend/tests/modules/signals/test_api.py`
- Create: `backend/tests/modules/signals/test_projector.py`
- Create: `backend/tests/integration/test_signal_job_handlers.py`
- Modify: `backend/tests/integration/test_worker_queue_isolation.py`

- [ ] **Step 1: Write failing API and worker tests**

Test all read routes, reset/reevaluate authentication, required idempotency and concrete schemas. Projector tests assert a signal job is created only from committed `quote_cycle.finalized`, `target.activated` and `position.became_holding` events. Worker tests assert only eligible item IDs are processed, per-stock failure is isolated, versions are frozen and stale retries are rejected.

```python
@pytest.mark.anyio
async def test_projector_turns_finalized_quote_event_into_one_signal_job() -> None:
    await store_event("quote_cycle.finalized", valid_item_ids=[str(valid_item.id)])
    report = await projector.project_once()
    assert report.projected == 1
    assert submitted.job_type == "SIGNAL_EVALUATE_BATCH"
    assert submitted.config_snapshot["eligible_item_ids"] == [str(valid_item.id)]
```

- [ ] **Step 2: Run API/worker tests and verify RED**

```text
cd backend
uv run pytest -q tests/modules/signals/test_api.py tests/modules/signals/test_projector.py tests/integration/test_signal_job_handlers.py tests/integration/test_worker_queue_isolation.py
```

Expected: routes and handlers are missing.

- [ ] **Step 3: Implement signal routes**

Implement exactly:

```text
GET  /api/v1/signals/states
GET  /api/v1/signals/states/{subscription_id}
GET  /api/v1/signal-events
GET  /api/v1/signal-events/{event_id}
GET  /api/v1/signal-evaluations
GET  /api/v1/signal-evaluations/{evaluation_id}
POST /api/v1/signals/states/{subscription_id}/reset
POST /api/v1/signals/states/{subscription_id}/reevaluate
```

Reset requires reason, `confirm=true`, expected state version and `Idempotency-Key`; it writes UNKNOWN plus a reevaluation request, never accepts a user-selected zone.

- [ ] **Step 4: Implement job handlers**

Register `SIGNAL_EVALUATE_BATCH` and `SIGNAL_REEVALUATE` on a dedicated `signals` queue. Batch handler iterates frozen eligible items, calls the public signal application per item and returns `PARTIAL` when some stocks fail. A failed stock cannot roll back another stock.

- [ ] **Step 5: Project public domain events into signal jobs**

`SignalEventProjector` claims only the three supported topics from the shared transaction outbox with `FOR UPDATE SKIP LOCKED`. In the same transaction it submits the corresponding logical Job through `JobService` and marks the source event dispatched. The deterministic job key is derived from topic plus source event ID, so projector retries cannot duplicate work. It reads event payloads only and never queries or modifies quote, target or position tables.

- [ ] **Step 6: Add isolated projector and signal worker**

Add `signal-projector` running `python -m long_invest.entrypoints.signal_projector` and `worker-signals` running `python -m long_invest.entrypoints.job_worker signals`. Both have no public port, read-only root filesystem, `no-new-privileges`, bounded memory and the shared application log volume. One failed projection is logged and released for bounded retry without terminating the process.

- [ ] **Step 7: Run the integration gate**

```text
cd backend
uv run pytest -q tests/modules/signals/test_api.py tests/modules/signals/test_projector.py tests/integration/test_signal_job_handlers.py tests/integration/test_worker_queue_isolation.py
uv run ruff check src/long_invest/modules/signals src/long_invest/bootstrap/jobs.py src/long_invest/entrypoints/signal_projector.py tests/modules/signals tests/integration/test_signal_job_handlers.py
```

Expected: selected tests pass.

- [ ] **Step 8: Commit API and workers**

```text
git add backend/src/long_invest/modules/signals backend/src/long_invest/bootstrap/jobs.py backend/src/long_invest/entrypoints/job_worker.py backend/src/long_invest/entrypoints/signal_projector.py deploy/compose.yaml backend/tests
git commit -m "feat: evaluate signals from domain events"
```

### Task 9: Serial integration, full acceptance and deployment

**Files:**
- Modify: `backend/src/long_invest/bootstrap/app.py`
- Modify: `backend/openapi.json`
- Modify: `frontend/src/shared/api/generated/schema.d.ts`
- Create: `backend/tests/integration/test_targets_signals_stage3.py`

- [ ] **Step 1: Write failing main-app contract test**

Assert every target/signal route exists, all 2xx responses reference concrete schemas, all writes expose required `Idempotency-Key`, saved OpenAPI equals runtime OpenAPI and operation IDs are unique.

- [ ] **Step 2: Register only public routers**

Import and include `targets.api.router` and `signals.api.router` in `bootstrap/app.py`. Bootstrap must not import either module's models or repository.

- [ ] **Step 3: Export OpenAPI and regenerate frontend types**

```text
cd backend
uv run python -m long_invest.entrypoints.export_openapi
cd ../frontend
npm run generate:api
```

- [ ] **Step 4: Run targeted integration before full acceptance**

```text
cd backend
uv run pytest -q tests/modules/targets tests/modules/signals tests/integration/test_targets_signals_stage3.py
uv run ruff check src/long_invest/modules/targets src/long_invest/modules/signals tests/modules/targets tests/modules/signals
git diff --check
```

Expected: target/signal tests pass before any full-suite run.

- [ ] **Step 5: Run one backend full acceptance**

```text
cd backend
uv run pytest -q
uv run ruff check .
uv run python -m compileall -q src
uv run alembic heads
```

Expected: all tests pass and the only migration head is `20260717_0011`.

- [ ] **Step 6: Run one frontend full acceptance**

```text
cd frontend
npm test -- --run
npm run lint
npm run typecheck
npm run build
```

Expected: tests, lint, type checking and production build pass; generated types contain concrete target and signal responses.

- [ ] **Step 7: Run one fresh-container full acceptance**

Use a new Compose project and volume. Upgrade from empty PostgreSQL, enable target/signal PostgreSQL concurrency flags, run the full test service once, verify application-role permissions, then remove the project and volumes.

- [ ] **Step 8: Commit integration artifacts**

```text
git add backend/src/long_invest/bootstrap/app.py backend/openapi.json frontend/src/shared/api/generated/schema.d.ts backend/tests/integration/test_targets_signals_stage3.py
git commit -m "feat: integrate stage3 target signal loop"
```

- [ ] **Step 9: Push and deploy once**

```text
git push server main
ssh tencent-ubuntu "cd /home/ubuntu/my_git/LongInvest && docker compose -f deploy/compose.yaml up -d --build"
```

Verify migration `20260717_0011`, public frontend and `/health/ready` return 200, unauthenticated target/signal routes return 401, signal worker restart count is zero, and no new public ports exist.

## Completion gate

The stage is complete only when:

- target/signal contracts and the single migration are frozen before domain work;
- target writes atomically include binding, immutable revision, subscription mode switch, audit and reevaluation outbox;
- every formal signal attempt writes an evaluation, while skipped/superseded inputs cannot update state;
- signal state/event/notification writes are one transaction and concurrent evaluation produces one transition;
- quote cycles never evaluate before batch finalization;
- intermediate tasks use only minimal verification;
- exactly one full backend/frontend/container acceptance occurs after the complete stage is integrated;
- local `main`, `server/main` and the deployed server point to the same clean commit.
