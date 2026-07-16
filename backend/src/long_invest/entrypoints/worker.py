import socket

from redis import Redis
from rq import Queue, Worker

from long_invest.platform.config.settings import get_settings
from long_invest.platform.logging.configure import configure_logging


def main() -> None:
    settings = get_settings()
    queue_names = tuple(
        name.strip() for name in settings.worker_queues.split(",") if name.strip()
    )
    if not queue_names:
        raise ValueError("at least one worker queue is required")
    worker_role = "-".join(queue_names)
    configure_logging(
        level=settings.log_level,
        queue_capacity=settings.log_queue_capacity,
        log_file=settings.log_file,
        service=f"longinvest-worker-{worker_role}",
    )
    redis = Redis.from_url(settings.redis_url)
    worker = Worker(
        [Queue(name, connection=redis) for name in queue_names],
        connection=redis,
        name=f"{worker_role}-{socket.gethostname()}",
    )
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()
