# Jobs and Outbox Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the PostgreSQL-owned job, run, item, and transactional outbox foundation that reliably dispatches minimal messages to isolated RQ queues and rejects stale Worker results.

**Architecture:** Domain state remains in PostgreSQL. `JobService` writes `job` and `event_outbox` through a caller-owned transaction; a leased dispatcher sends deterministic RQ messages and then marks the database task queued. Worker and watchdog services update state only through fenced service methods, so Redis loss, duplicate delivery, process loss, and late completion cannot corrupt formal state.

**Tech Stack:** Python 3.12, SQLAlchemy 2 async, PostgreSQL 16, Redis 7, RQ 2.x, Alembic, pytest, Docker Compose.

---

## File map

- `platform/jobs/models.py`: owns `job`, `job_run`, and `job_item` tables and status enums.
- `platform/jobs/contracts.py`: owns immutable submit commands, `JobResult`, progress, and dispatcher result types.
- `platform/jobs/repository.py`: contains row locking and persistence primitives; it does not call Redis.
- `platform/jobs/service.py`: owns submit, idempotency, state transitions, run fencing, progress, and terminal results.
- `platform/outbox/models.py`: owns the reliable `event_outbox` table.
- `platform/outbox/repository.py`: claims leases, records dispatch success, and schedules bounded retry.
- `platform/outbox/dispatcher.py`: calls the queue adapter outside database transactions and finalizes results.
- `platform/queue/rq.py`: the only RQ-specific adapter; messages contain only database job and outbox IDs.
- `platform/jobs/watchdog.py`: resets stale outbox leases and marks confirmed stale runs lost.
- `entrypoints/dispatcher.py`, `entrypoints/watchdog.py`: long-running process loops.
- `alembic/versions/20260714_0004_jobs_outbox.py`: creates tables, constraints, indexes, grants, and retention-ready timestamps.

### Task 1: Add the RQ dependency and status contracts

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Create: `backend/src/long_invest/platform/jobs/__init__.py`
- Create: `backend/src/long_invest/platform/jobs/contracts.py`
- Test: `backend/tests/platform/jobs/test_contracts.py`

- [x] **Step 1: Write the failing contract tests**

Define tests that require `JobResult` to serialize only safe fields and require every V3.1 logical, run, and item status to exist as a string enum. Verify `JobResult.failure(...)` carries `retryable`, warnings, and duration metrics but never accepts an exception object.

- [x] **Step 2: Run the focused tests and confirm collection fails**

Run `pytest -q tests/platform/jobs/test_contracts.py`. Expected: import failure because `platform.jobs.contracts` does not exist.

- [x] **Step 3: Implement minimal immutable contracts**

Use `StrEnum` for the exact V3.1 states. Use frozen dataclasses for `JobResult`, `SubmitJob`, and `JobProgress`; all payload fields are JSON-compatible mappings and identifiers, not exception instances.

- [x] **Step 4: Add RQ 2.9 and refresh the lock file**

Add `rq>=2.9,<3` and run `uv lock`. RQ is isolated behind the queue adapter; domain services must not import RQ.

- [x] **Step 5: Run focused tests and commit**

Run the contract tests and Ruff. Commit `feat: add job contracts`.

### Task 2: Create job, run, item, and outbox storage

**Files:**
- Create: `backend/src/long_invest/platform/jobs/models.py`
- Create: `backend/src/long_invest/platform/outbox/__init__.py`
- Create: `backend/src/long_invest/platform/outbox/models.py`
- Modify: `backend/alembic/env.py`
- Create: `backend/alembic/versions/20260714_0004_jobs_outbox.py`
- Test: `backend/tests/platform/jobs/test_models.py`

- [x] **Step 1: Write database tests before models**

Require a `job` with type, queue, priority, frozen configuration, scoped idempotency key and request hash; multiple immutable `job_run` attempts; unique per-job `job_item` keys; and a pending `event_outbox` row. Assert duplicate scoped idempotency keys and duplicate `(job_id, attempt_no)` pairs are rejected by PostgreSQL.

- [x] **Step 2: Run focused tests and confirm missing imports**

Run `pytest -q tests/platform/jobs/test_models.py`. Expected: model import failure.

- [x] **Step 3: Implement the four SQLAlchemy models**

Use UUID primary keys, timezone-aware timestamps, JSONB snapshots/results, integer optimistic versions, explicit check constraints, and indexes for pending dispatch, active runs, job status, and cleanup timestamps. Store status values as bounded strings so migrations stay explicit.

- [x] **Step 4: Add a single linear migration**

Create all four tables in dependency order. Grant the application role normal job/outbox access while keeping table ownership with the migration role. Downgrade in reverse order.

- [x] **Step 5: Apply the migration and verify constraints**

Run `alembic upgrade head`, focused database tests, and `alembic check`. Commit `feat: add job and outbox storage`.

### Task 3: Submit jobs atomically and enforce idempotency

**Files:**
- Create: `backend/src/long_invest/platform/jobs/repository.py`
- Create: `backend/src/long_invest/platform/outbox/repository.py`
- Create: `backend/src/long_invest/platform/jobs/service.py`
- Test: `backend/tests/platform/jobs/test_service.py`

- [x] **Step 1: Write service tests against real PostgreSQL**

Submit through a caller-owned transaction and assert one `job` plus one outbox row appear together. Roll back the transaction and assert neither remains. Resubmit the same scope/key/hash and require the existing job; resubmit the same scope/key with a different hash and require HTTP 409 `IDEMPOTENCY_KEY_REUSED`.

- [x] **Step 2: Verify the tests fail because the service is absent**

Run `pytest -q tests/platform/jobs/test_service.py` and confirm the missing service is the failure reason.

- [x] **Step 3: Implement `JobService.submit`**

Accept an existing `AsyncSession`; never call commit. Canonically hash the frozen request with sorted JSON. Insert the logical job and a minimal outbox payload containing `job_id`, `outbox_id`, type, queue, and request ID. Resolve unique-key races by re-reading and comparing the stored hash.

- [x] **Step 4: Verify rollback, replay, and conflict behavior**

Run the focused tests and the existing audit tests. Commit `feat: submit jobs through transactional outbox`.

### Task 4: Lease outbox rows and dispatch deterministic RQ jobs

**Files:**
- Create: `backend/src/long_invest/platform/queue/__init__.py`
- Create: `backend/src/long_invest/platform/queue/rq.py`
- Create: `backend/src/long_invest/platform/outbox/dispatcher.py`
- Test: `backend/tests/platform/outbox/test_dispatcher.py`

- [x] **Step 1: Write dispatcher tests with a narrow queue fake**

Test that two dispatcher instances cannot claim the same row; Redis failure returns the row to pending with a future retry time and leaves the job `PENDING_DISPATCH`; success records the RQ ID and changes the job to `QUEUED`; replay uses deterministic `outbox-{outbox_id}` and does not create a second RQ job.

- [x] **Step 2: Run focused tests and confirm missing dispatcher failure**

Run `pytest -q tests/platform/outbox/test_dispatcher.py`.

- [x] **Step 3: Implement lease and finalization transactions**

Claim due rows using `FOR UPDATE SKIP LOCKED`, persist `DISPATCHING`, owner, lease time, and attempt count, then commit. Call Redis outside the transaction. Finalize success or a capped exponential retry in a new transaction.

- [x] **Step 4: Implement the RQ adapter**

Use one sync Redis connection and `Queue.enqueue` with `job_id=f"outbox-{outbox_id}"`, `unique=True`, explicit timeout, and only `job_id`/`outbox_id` arguments. Never use RQ status as business truth.

- [x] **Step 5: Verify success, duplicate, and Redis-down cases**

Run focused tests with real PostgreSQL plus Redis adapter integration, then commit `feat: add reliable outbox dispatcher`.

### Task 5: Add fenced run lifecycle and safe results

**Files:**
- Modify: `backend/src/long_invest/platform/jobs/repository.py`
- Modify: `backend/src/long_invest/platform/jobs/service.py`
- Create: `backend/src/long_invest/platform/jobs/worker.py`
- Test: `backend/tests/platform/jobs/test_run_lifecycle.py`

- [ ] **Step 1: Write lifecycle tests**

Require attempt numbers to increase, run rows to remain immutable after terminal state, heartbeats to update only the active fence, progress to reject stale fences, terminal completion to update job and run atomically, and a late old fence to become `SUPERSEDED` without overwriting the new result.

- [ ] **Step 2: Confirm focused tests fail on missing lifecycle methods**

Run `pytest -q tests/platform/jobs/test_run_lifecycle.py`.

- [ ] **Step 3: Implement fenced state transitions**

Lock the logical job before creating a run. Generate a UUID fence token for every attempt and compare it with `job.current_fence_token` on heartbeat, progress, completion, and failure. Store only `JobResult` safe summaries.

- [ ] **Step 4: Run lifecycle and full job tests**

Run all `tests/platform/jobs` tests and commit `feat: fence worker run updates`.

### Task 6: Add watchdog recovery and process entrypoints

**Files:**
- Create: `backend/src/long_invest/platform/jobs/watchdog.py`
- Create: `backend/src/long_invest/entrypoints/dispatcher.py`
- Create: `backend/src/long_invest/entrypoints/watchdog.py`
- Modify: `backend/src/long_invest/platform/config/settings.py`
- Modify: `deploy/compose.yaml`
- Test: `backend/tests/platform/jobs/test_watchdog.py`

- [ ] **Step 1: Write recovery tests**

Require expired outbox leases to return to pending, active runs with heartbeats newer than 60 seconds to remain untouched, stale runs to become `LOST`, and only one automatic recovery run to be scheduled when policy allows it.

- [ ] **Step 2: Verify focused tests fail**

Run `pytest -q tests/platform/jobs/test_watchdog.py`.

- [ ] **Step 3: Implement bounded recovery**

Use database time for lease and heartbeat comparisons. Recovery methods lock rows and are idempotent. The loops use configurable scan intervals, structured maintenance logs, and graceful cancellation.

- [ ] **Step 4: Add isolated Compose roles**

Add `dispatcher` and `watchdog` services from the same backend image. Give both the low-privilege application database URL; only the dispatcher receives Redis. Keep read-only filesystems, memory limits, bounded logs, and no host ports.

- [ ] **Step 5: Run recovery tests and commit**

Commit `feat: add dispatcher and watchdog processes`.

### Task 7: End-to-end reliability verification

**Files:**
- Create: `backend/tests/integration/test_jobs_outbox_flow.py`
- Modify: `docs/superpowers/plans/2026-07-14-jobs-outbox-foundation.md`

- [ ] **Step 1: Add an end-to-end test**

Submit a representative maintenance job, dispatch it, verify the deterministic RQ record, claim a fenced run, complete it, and confirm the database result. Add a second test that submits while Redis is stopped and dispatches successfully after Redis returns without creating a second logical job.

- [ ] **Step 2: Run all automated gates**

Run `pytest -q`, `ruff check .`, and `alembic check`. Expected: zero failures and no pending migration operations.

- [ ] **Step 3: Run server container fault checks**

Rebuild services, verify API/dispatcher/watchdog health, stop Redis, submit and confirm `PENDING_DISPATCH`, restore Redis and confirm `QUEUED`, then verify only one deterministic RQ message exists.

- [ ] **Step 4: Mark every completed checkbox and commit**

Commit `test: verify jobs outbox reliability`. Push the verified main branch to the server workspace and configured GitHub remote.

---

## Acceptance boundary

This plan does not add a public generic job creation endpoint, business-specific handlers, pauseable bulk processing, notification retry policy, strategy execution, or market-data logic. It delivers only the shared reliable task substrate required by those modules.
