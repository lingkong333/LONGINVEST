from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select

from long_invest.platform.database.engine import Database
from long_invest.platform.jobs.contracts import JobRunStatus, JobStatus
from long_invest.platform.jobs.models import JobRun
from long_invest.platform.jobs.repository import JobRepository
from long_invest.platform.outbox.models import EventOutbox, OutboxStatus


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    outbox_leases_released: int
    runs_lost: int
    recoveries_scheduled: int


class JobsWatchdog:
    def __init__(
        self,
        *,
        database: Database,
        outbox_lease_timeout: timedelta = timedelta(seconds=60),
        run_stale_timeout: timedelta = timedelta(seconds=60),
        batch_size: int = 100,
    ) -> None:
        self._database = database
        self._outbox_lease_timeout = outbox_lease_timeout
        self._run_stale_timeout = run_stale_timeout
        self._batch_size = batch_size

    async def recover_once(self) -> RecoveryReport:
        async with self._database.transaction() as session:
            now = await session.scalar(select(func.now()))
            if now is None:
                raise RuntimeError("database time is unavailable")
            released = await self._release_outbox_leases(session, now)
            lost, scheduled = await self._recover_stale_runs(session, now)
        return RecoveryReport(
            outbox_leases_released=released,
            runs_lost=lost,
            recoveries_scheduled=scheduled,
        )

    async def _release_outbox_leases(self, session, now) -> int:
        cutoff = now - self._outbox_lease_timeout
        events = (
            await session.scalars(
                select(EventOutbox)
                .where(
                    EventOutbox.status == OutboxStatus.DISPATCHING,
                    EventOutbox.locked_at < cutoff,
                )
                .order_by(EventOutbox.locked_at)
                .limit(self._batch_size)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for event in events:
            event.status = OutboxStatus.PENDING
            event.locked_at = None
            event.locked_by = None
            event.next_attempt_at = now
            event.last_error_code = "DISPATCH_LEASE_EXPIRED"
            event.last_error_summary = "分发租约过期，等待重新分发"
        return len(events)

    async def _recover_stale_runs(self, session, now) -> tuple[int, int]:
        cutoff = now - self._run_stale_timeout
        last_activity = func.coalesce(
            JobRun.heartbeat_at,
            JobRun.started_at,
            JobRun.claimed_at,
        )
        runs = (
            await session.scalars(
                select(JobRun)
                .where(
                    JobRun.status.in_(
                        (
                            JobRunStatus.CLAIMED,
                            JobRunStatus.STARTING,
                            JobRunStatus.RUNNING,
                        )
                    ),
                    last_activity < cutoff,
                )
                .order_by(last_activity)
                .limit(self._batch_size)
                .with_for_update(skip_locked=True)
            )
        ).all()
        scheduled = 0
        jobs = JobRepository(session)
        for run in runs:
            job = await jobs.lock(run.job_id)
            if (
                job is None
                or job.current_run_id != run.id
                or job.current_fence_token != run.fence_token
            ):
                run.status = JobRunStatus.SUPERSEDED
                run.ended_at = run.ended_at or now
                continue
            run.status = JobRunStatus.LOST
            run.ended_at = now
            run.exit_type = "HEARTBEAT_LOST"
            lost_count = await session.scalar(
                select(func.count()).select_from(JobRun).where(
                    JobRun.job_id == job.id,
                    JobRun.status == JobRunStatus.LOST,
                )
            )
            if lost_count == 1:
                await self._schedule_recovery(session, jobs, job, run, now)
                scheduled += 1
            else:
                job.status = JobStatus.LOST
                job.current_run_id = None
                job.current_fence_token = None
                job.terminal_at = now
                job.updated_at = now
                job.version += 1
        await session.flush()
        return len(runs), scheduled

    async def _schedule_recovery(self, session, jobs, job, lost_run, now) -> None:
        recovery_run = JobRun(
            job_id=job.id,
            attempt_no=await jobs.next_attempt_no(job.id),
            worker_id=None,
            fence_token=uuid4(),
            status=JobRunStatus.CLAIMED,
            soft_timeout_seconds=lost_run.soft_timeout_seconds,
            hard_timeout_seconds=lost_run.hard_timeout_seconds,
        )
        session.add(recovery_run)
        await session.flush()
        outbox_id = uuid4()
        session.add(
            EventOutbox(
                id=outbox_id,
                topic="jobs.dispatch",
                aggregate_type="job",
                aggregate_id=str(job.id),
                queue=job.queue,
                payload={
                    "job_id": str(job.id),
                    "outbox_id": str(outbox_id),
                    "job_type": job.job_type,
                    "queue": job.queue,
                    "request_id": job.request_id,
                    "recovery_run_id": str(recovery_run.id),
                },
                dedupe_key=f"job-recovery:{lost_run.id}",
                status=OutboxStatus.PENDING,
            )
        )
        job.status = JobStatus.WAITING_RETRY
        job.current_run_id = recovery_run.id
        job.current_fence_token = recovery_run.fence_token
        job.updated_at = now
        job.version += 1
