from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI

from long_invest.platform.errors import AppError
from long_invest.platform.jobs.admin import JobPage
from long_invest.platform.jobs.api import (
    JobCommandBody,
    _command_route,
    list_jobs,
    router,
)
from long_invest.platform.jobs.contracts import JobStatus


class Application:
    def __init__(self) -> None:
        self.command_call = None
        self.job = SimpleNamespace(id=uuid4(), status=JobStatus.PAUSING, version=2)

    async def list_jobs(self, **_filters):
        return JobPage(items=(), page=1, page_size=50, total=0)

    async def command(self, job_id, action, context):
        self.command_call = (job_id, action, context)
        self.job.id = job_id
        return self.job

    async def allowed_actions(self, _job_id):
        return ("cancel",)


def _identity():
    return SimpleNamespace(
        user=SimpleNamespace(id=uuid4()),
        session=SimpleNamespace(id=uuid4()),
        audit_context=SimpleNamespace(request_id="req-1", trusted_ip="127.0.0.1"),
    )


def test_list_api_returns_empty_page() -> None:
    async def scenario() -> None:
        response = await list_jobs(Application(), SimpleNamespace())
        assert response["data"] == {
            "items": [],
            "pagination": {"page": 1, "page_size": 50, "total": 0},
        }

    asyncio.run(scenario())


def test_pause_api_passes_version_idempotency_and_reason() -> None:
    async def scenario() -> None:
        application = Application()
        job_id = uuid4()
        endpoint = _command_route("pause")
        response = await endpoint(
            job_id,
            JobCommandBody(confirm=True, reason=" 暂停批量任务 ", expected_version=1),
            application,
            _identity(),
            "pause-key",
        )

        assert response["code"] == "JOB_ACCEPTED"
        assert response["data"]["status"] == "PAUSING"
        context = application.command_call[2]
        assert context.expected_version == 1
        assert context.idempotency_key == "pause-key"
        assert context.reason == "暂停批量任务"

    asyncio.run(scenario())


def test_command_requires_explicit_confirmation() -> None:
    async def scenario() -> None:
        endpoint = _command_route("cancel")
        with pytest.raises(AppError) as captured:
            await endpoint(
                uuid4(),
                JobCommandBody(confirm=False, reason="取消", expected_version=1),
                Application(),
                _identity(),
                "cancel-key",
            )
        assert captured.value.code == "AUTH_CONFIRMATION_REQUIRED"

    asyncio.run(scenario())


def test_router_exposes_management_surface_without_generic_create() -> None:
    methods = {
        (route.path, method) for route in router.routes for method in route.methods
    }
    assert {
        ("/api/v1/jobs", "GET"),
        ("/api/v1/jobs/{job_id}", "GET"),
        ("/api/v1/jobs/{job_id}/runs", "GET"),
        ("/api/v1/jobs/{job_id}/items", "GET"),
        ("/api/v1/jobs/{job_id}/allowed-actions", "GET"),
        ("/api/v1/jobs/{job_id}/cancel", "POST"),
        ("/api/v1/jobs/{job_id}/pause", "POST"),
        ("/api/v1/jobs/{job_id}/resume", "POST"),
        ("/api/v1/jobs/{job_id}/retry", "POST"),
        ("/api/v1/jobs/{job_id}/retry-failed-items", "POST"),
    } <= methods
    assert ("/api/v1/jobs", "POST") not in methods


def test_write_routes_publish_required_idempotency_header() -> None:
    app = FastAPI()
    app.include_router(router)
    paths = app.openapi()["paths"]
    for action in ("cancel", "pause", "resume", "retry", "retry-failed-items"):
        operation = paths[f"/api/v1/jobs/{{job_id}}/{action}"]["post"]
        header = next(
            item
            for item in operation["parameters"]
            if item["in"] == "header" and item["name"] == "Idempotency-Key"
        )
        assert header["required"] is True
