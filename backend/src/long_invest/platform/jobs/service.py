import hashlib
import json
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import JobStatus, SubmitJob
from long_invest.platform.jobs.models import Job
from long_invest.platform.jobs.repository import JobRepository
from long_invest.platform.outbox.models import EventOutbox, OutboxStatus


class JobService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._jobs = JobRepository(session)

    async def submit(self, command: SubmitJob) -> Job:
        request_hash = _request_hash(command)
        existing = await self._jobs.find_by_idempotency(
            scope=command.idempotency_scope,
            key=command.idempotency_key,
        )
        if existing is not None:
            return _resolve_replay(existing, request_hash)

        job_id = uuid4()
        outbox_id = uuid4()
        job = Job(
            id=job_id,
            job_type=command.job_type,
            business_object_type=command.business_object_type,
            business_object_id=command.business_object_id,
            queue=command.queue,
            priority=command.priority,
            status=JobStatus.PENDING_DISPATCH,
            config_snapshot=command.config_snapshot,
            idempotency_scope=command.idempotency_scope,
            idempotency_key=command.idempotency_key,
            request_hash=request_hash,
            request_id=command.request_id,
            created_by_user_id=command.created_by_user_id,
        )
        event = EventOutbox(
            id=outbox_id,
            topic="jobs.dispatch",
            aggregate_type="job",
            aggregate_id=str(job_id),
            queue=command.queue,
            payload={
                "job_id": str(job_id),
                "outbox_id": str(outbox_id),
                "job_type": command.job_type,
                "queue": command.queue,
                "request_id": command.request_id,
            },
            dedupe_key=_outbox_dedupe_key(command),
            status=OutboxStatus.PENDING,
        )

        try:
            async with self._session.begin_nested():
                self._session.add_all((job, event))
                await self._session.flush()
        except IntegrityError:
            existing = await self._jobs.find_by_idempotency(
                scope=command.idempotency_scope,
                key=command.idempotency_key,
            )
            if existing is None:
                raise
            return _resolve_replay(existing, request_hash)
        return job


def _request_hash(command: SubmitJob) -> str:
    content = {
        "job_type": command.job_type,
        "queue": command.queue,
        "priority": command.priority,
        "business_object_type": command.business_object_type,
        "business_object_id": command.business_object_id,
        "created_by_user_id": command.created_by_user_id,
        "config_snapshot": command.config_snapshot,
    }
    serialized = json.dumps(
        content,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def _outbox_dedupe_key(command: SubmitJob) -> str:
    raw = f"{command.idempotency_scope}\0{command.idempotency_key}".encode()
    return f"job:{hashlib.sha256(raw).hexdigest()}"


def _resolve_replay(existing: Job, request_hash: str) -> Job:
    if existing.request_hash != request_hash:
        raise AppError(
            code="IDEMPOTENCY_KEY_REUSED",
            message="同一幂等键不能用于不同任务内容",
            status_code=409,
            details={"existing_job_id": str(existing.id)},
        )
    return existing
