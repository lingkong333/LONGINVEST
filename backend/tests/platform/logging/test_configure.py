import json
from io import StringIO

import structlog
from asgi_correlation_id import correlation_id

from long_invest.platform.logging.configure import configure_logging


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
