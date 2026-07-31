from __future__ import annotations

from uuid import uuid4

import pytest

from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.admin import JobAdminService, JobCommandContext
from long_invest.platform.jobs.contracts import JobStatus
from long_invest.platform.jobs.models import Job


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _legacy_job(*, suffix: str) -> Job:
    return Job(
        id=uuid4(),
        job_type="ADMIN_TEST",
        queue="maintenance",
        status=JobStatus.CANCELED,
        config_snapshot={"sample": suffix},
        idempotency_scope=f"admin-test:{suffix}",
        idempotency_key=f"create:{suffix}",
        request_hash=uuid4().hex,
        request_id=f"req-{suffix}",
        soft_timeout_seconds=30,
        hard_timeout_seconds=60,
    )


def _context(key: str) -> JobCommandContext:
    return JobCommandContext(
        request_id=f"req-{key}",
        idempotency_key=key,
        actor_user_id="admin-test",
        reason="验证旧版任务只读",
        expected_version=1,
    )


@pytest.mark.anyio
async def test_list_jobs_returns_empty_page_for_unmatched_filter() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    try:
        async with database.session() as session:
            page = await JobAdminService(session).list_jobs(
                page=1, page_size=10, job_type=f"missing-{uuid4().hex}"
            )

        assert page.items == ()
        assert page.total == 0
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_legacy_job_is_visible_but_cannot_be_controlled() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    suffix = uuid4().hex
    job = _legacy_job(suffix=suffix)
    try:
        async with database.transaction() as session:
            session.add(job)

        async with database.session() as session:
            service = JobAdminService(session)
            stored = await service.get_job(job.id)
            actions = await service.allowed_actions(job.id)
            assert stored.id == job.id
            assert actions == ()

        with pytest.raises(AppError) as error:
            async with database.transaction() as session:
                await JobAdminService(session).command(
                    job.id,
                    "retry",
                    _context(f"retry-{suffix}"),
                )

        assert error.value.code == "LEGACY_JOB_READ_ONLY"
    finally:
        await database.dispose()
