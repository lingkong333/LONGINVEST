from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from long_invest.platform.jobs.service import JobService


@pytest.mark.anyio
async def test_job_service_exposes_read_by_id_without_internal_model_access() -> None:
    job_id = uuid4()
    expected = SimpleNamespace(id=job_id)
    service = JobService(AsyncMock())
    service._jobs.get = AsyncMock(return_value=expected)

    result = await service.get(job_id)

    assert result is expected
    service._jobs.get.assert_awaited_once_with(job_id)
