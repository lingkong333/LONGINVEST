from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from long_invest.bootstrap import jobs
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import JobExecutionContext


def _context(config):
    return JobExecutionContext(job_id=uuid4(), fence_token=uuid4(), config=config)


@pytest.mark.anyio
async def test_signal_batch_processes_only_frozen_eligible_items(monkeypatch):
    cycle_id, first, second = uuid4(), uuid4(), uuid4()
    report = SimpleNamespace(
        succeeded=2,
        failed=0,
        items=(
            SimpleNamespace(item_id=first, success=True, code="SIGNAL_APPLIED"),
            SimpleNamespace(item_id=second, success=True, code="SIGNAL_UNCHANGED"),
        ),
    )
    application = SimpleNamespace(evaluate_batch=AsyncMock(return_value=report))
    monkeypatch.setattr(jobs, "_signal_job_application", lambda: application)

    result = await jobs.signal_evaluate_batch(
        _context(
            {
                "quote_cycle_id": str(cycle_id),
                "eligible_item_ids": [str(first), str(second)],
            }
        )
    )

    assert result.success is True
    assert result.code == "SUCCESS"
    application.evaluate_batch.assert_awaited_once()
    call = application.evaluate_batch.await_args.kwargs
    assert call["cycle_id"] == cycle_id
    assert call["item_ids"] == (first, second)


@pytest.mark.anyio
async def test_signal_batch_reports_partial_without_losing_successes(monkeypatch):
    report = SimpleNamespace(
        succeeded=1,
        failed=1,
        items=(
            SimpleNamespace(item_id=uuid4(), success=True, code="SIGNAL_APPLIED"),
            SimpleNamespace(
                item_id=uuid4(), success=False, code="SIGNAL_BACKEND_UNAVAILABLE"
            ),
        ),
    )
    application = SimpleNamespace(evaluate_batch=AsyncMock(return_value=report))
    monkeypatch.setattr(jobs, "_signal_job_application", lambda: application)

    result = await jobs.signal_evaluate_batch(
        _context(
            {
                "quote_cycle_id": str(uuid4()),
                "eligible_item_ids": [str(item.item_id) for item in report.items],
            }
        )
    )

    assert result.success is True
    assert result.code == "PARTIAL"
    assert result.data["succeeded"] == 1
    assert result.data["failed"] == 1


@pytest.mark.anyio
async def test_signal_batch_rejects_invalid_or_duplicate_frozen_scope():
    item_id = uuid4()
    result = await jobs.signal_evaluate_batch(
        _context(
            {
                "quote_cycle_id": str(uuid4()),
                "eligible_item_ids": [str(item_id), str(item_id)],
            }
        )
    )
    assert result.success is False
    assert result.code == "SIGNAL_BATCH_CONFIG_INVALID"
    assert result.retryable is False


@pytest.mark.anyio
async def test_signal_reevaluate_forwards_frozen_versions(monkeypatch):
    outcome = SimpleNamespace(code="SIGNAL_APPLIED")
    application = SimpleNamespace(reevaluate=AsyncMock(return_value=outcome))
    monkeypatch.setattr(jobs, "_signal_job_application", lambda: application)
    context = _context(
        {
            "subscription_id": str(uuid4()),
            "reason": "TARGET_ACTIVATED",
            "target_revision_id": str(uuid4()),
            "target_binding_version": 4,
        }
    )

    result = await jobs.signal_reevaluate(context)

    assert result.success is True
    application.reevaluate.assert_awaited_once_with(
        config=context.config,
        request_id=str(context.job_id),
        idempotency_key=f"signal-job:{context.job_id}",
    )


@pytest.mark.anyio
async def test_signal_reevaluate_rejects_stale_retry_without_retrying(monkeypatch):
    application = SimpleNamespace(
        reevaluate=AsyncMock(
            side_effect=AppError(
                code="SIGNAL_INPUT_SUPERSEDED",
                message="stale",
                status_code=409,
            )
        )
    )
    monkeypatch.setattr(jobs, "_signal_job_application", lambda: application)

    result = await jobs.signal_reevaluate(
        _context(
            {
                "subscription_id": str(uuid4()),
                "reason": "TARGET_ACTIVATED",
            }
        )
    )

    assert result.success is False
    assert result.code == "SIGNAL_INPUT_SUPERSEDED"
    assert result.retryable is False
