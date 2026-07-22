import os

from redis import Redis
from rq import Queue
from rq.worker_pool import WorkerPool

from long_invest.platform.config.settings import get_settings
from long_invest.platform.logging.configure import configure_logging


def main() -> None:
    settings = get_settings()
    concurrency = _concurrency(os.getenv("LONGINVEST_BACKTEST_CONCURRENCY", "4"))
    configure_logging(
        level=settings.log_level,
        queue_capacity=settings.log_queue_capacity,
        log_file=settings.log_file,
        service="longinvest-worker-bulk-backtest",
    )
    redis = Redis.from_url(settings.redis_url)
    WorkerPool(
        [Queue("bulk-backtest", connection=redis)],
        connection=redis,
        num_workers=concurrency,
    ).start()


def _concurrency(value: str) -> int:
    try:
        concurrency = int(value)
    except ValueError as exc:
        raise ValueError("backtest concurrency must be an integer") from exc
    if not 1 <= concurrency <= 8:
        raise ValueError("backtest concurrency must be between 1 and 8")
    return concurrency


if __name__ == "__main__":
    main()
