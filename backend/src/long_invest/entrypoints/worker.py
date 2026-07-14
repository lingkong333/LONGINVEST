import socket

from redis import Redis
from rq import Queue, Worker

from long_invest.platform.config.settings import get_settings
from long_invest.platform.logging.configure import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        queue_capacity=settings.log_queue_capacity,
        log_file=settings.log_file,
        service="longinvest-worker-maintenance",
    )
    redis = Redis.from_url(settings.redis_url)
    worker = Worker(
        [Queue("maintenance", connection=redis)],
        connection=redis,
        name=f"maintenance-{socket.gethostname()}",
    )
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()
