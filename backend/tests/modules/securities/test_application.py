from contextlib import asynccontextmanager
from unittest.mock import Mock
from uuid import uuid4

import pytest

from long_invest.modules.securities.application import SecurityApplication


class FakeDatabase:
    def __init__(self) -> None:
        self.session = Mock()

    @asynccontextmanager
    async def transaction(self):
        yield self.session


@pytest.mark.anyio
async def test_refresh_submits_public_job_in_database_transaction() -> None:
    database = FakeDatabase()
    captured = {}
    job = Mock(
        id=uuid4(),
        status="PENDING_DISPATCH",
        job_type="SECURITY_MASTER_REFRESH",
    )

    class FakeJobService:
        def __init__(self, session) -> None:
            captured["session"] = session

        async def submit(self, command):
            captured["command"] = command
            return job

    application = SecurityApplication(
        database,
        job_service_factory=FakeJobService,
    )

    submitted = await application.refresh(
        idempotency_key="refresh-key",
        request_id="request-1",
        created_by_user_id="user-1",
    )

    assert submitted is job
    assert captured["session"] is database.session
    command = captured["command"]
    assert command.job_type == "SECURITY_MASTER_REFRESH"
    assert command.queue == "maintenance"
    assert command.idempotency_scope == "securities:refresh"
    assert command.idempotency_key == "refresh-key"
    assert command.created_by_user_id == "user-1"
