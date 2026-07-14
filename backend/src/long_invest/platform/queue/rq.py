import asyncio
from uuid import UUID

from redis import Redis
from rq import Queue


class RqQueuePublisher:
    def __init__(self, redis_url: str) -> None:
        self._redis = Redis.from_url(redis_url)

    async def publish(
        self,
        *,
        queue: str,
        outbox_id: UUID,
        job_id: UUID,
        timeout_seconds: int,
    ) -> str:
        return await asyncio.to_thread(
            self._publish_sync,
            queue,
            outbox_id,
            job_id,
            timeout_seconds,
        )

    def _publish_sync(
        self,
        queue_name: str,
        outbox_id: UUID,
        job_id: UUID,
        timeout_seconds: int,
    ) -> str:
        rq_job_id = f"outbox-{outbox_id}"
        queue = Queue(queue_name, connection=self._redis)
        job = queue.enqueue(
            "long_invest.platform.jobs.worker.execute_job",
            str(job_id),
            str(outbox_id),
            job_id=rq_job_id,
            unique=True,
            job_timeout=timeout_seconds,
            result_ttl=86_400,
            failure_ttl=604_800,
        )
        return job.id

    async def close(self) -> None:
        await asyncio.to_thread(self._redis.close)
