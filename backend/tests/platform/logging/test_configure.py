import json
import logging
from io import StringIO
from queue import Queue

import structlog
from asgi_correlation_id import correlation_id

from long_invest.platform.logging.configure import (
    NonBlockingQueueHandler,
    configure_logging,
    get_dropped_log_counts,
)


def test_configure_logging_emits_json_with_request_id() -> None:
    stream = StringIO()
    configure_logging(level="INFO", stream=stream, use_queue=False)
    token = correlation_id.set("req_01J00000000000000000000000")
    try:
        structlog.get_logger("test").info("foundation_ready", component="api")
    finally:
        correlation_id.reset(token)

    record = json.loads(stream.getvalue())
    assert record["event"] == "foundation_ready"
    assert record["component"] == "api"
    assert record["level"] == "info"
    assert record["logger"] == "test"
    assert record["service"] == "longinvest-api"
    assert record["category"] == "application"
    assert record["message"] == "foundation_ready"
    assert record["request_id"] == "req_01J00000000000000000000000"
    assert record["timestamp"].endswith("Z")


def test_configure_logging_honors_log_level() -> None:
    stream = StringIO()
    configure_logging(level="WARNING", stream=stream, use_queue=False)

    logger = structlog.get_logger("test")
    logger.info("hidden")
    logger.warning("visible")

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [record["event"] for record in records] == ["visible"]


def test_configure_logging_accepts_process_service_name() -> None:
    stream = StringIO()
    configure_logging(
        level="INFO",
        stream=stream,
        use_queue=False,
        service="longinvest-dispatcher",
    )

    structlog.get_logger("test").info("cycle_complete")

    assert json.loads(stream.getvalue())["service"] == "longinvest-dispatcher"


def test_full_log_queue_drops_without_blocking() -> None:
    log_queue: Queue[logging.LogRecord] = Queue(maxsize=1)
    log_queue.put(logging.LogRecord("test", logging.INFO, "", 0, "first", (), None))
    handler = NonBlockingQueueHandler(log_queue, [])
    before = get_dropped_log_counts().get("INFO", 0)

    handler.emit(logging.LogRecord("test", logging.INFO, "", 0, "second", (), None))

    assert get_dropped_log_counts()["INFO"] == before + 1
