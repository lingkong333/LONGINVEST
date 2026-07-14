import atexit
import copy
import logging
import sys
from collections import Counter
from collections.abc import MutableMapping
from logging.handlers import QueueListener, TimedRotatingFileHandler
from pathlib import Path
from queue import Full, Queue
from threading import Lock
from typing import Any, TextIO

import structlog
from asgi_correlation_id import correlation_id
from structlog.stdlib import ProcessorFormatter

_listener: QueueListener | None = None
_listener_lock = Lock()
_dropped_logs: Counter[str] = Counter()


def add_request_id(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    if request_id := correlation_id.get():
        event_dict["request_id"] = request_id
    return event_dict


def add_log_contract(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    event = str(event_dict.get("event", "log_event"))
    event_dict.setdefault("service", "longinvest-api")
    event_dict.setdefault("category", "application")
    event_dict.setdefault("message", event)
    return event_dict


class NonBlockingQueueHandler(logging.Handler):
    def __init__(
        self,
        log_queue: Queue[logging.LogRecord],
        fallback_handlers: list[logging.Handler],
    ) -> None:
        super().__init__()
        self._queue = log_queue
        self._fallback_handlers = fallback_handlers

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._queue.put_nowait(copy.copy(record))
        except Full:
            _dropped_logs[record.levelname] += 1
            if record.levelno >= logging.WARNING:
                for handler in self._fallback_handlers:
                    handler.handle(record)


def configure_logging(
    *,
    level: str,
    stream: TextIO | None = None,
    use_queue: bool = True,
    queue_capacity: int = 10_000,
    log_file: str | None = None,
) -> None:
    global _listener

    log_level = getattr(logging, level.upper())
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        add_request_id,
        add_log_contract,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    formatter = ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    sinks = _build_sinks(formatter, stream=stream, log_file=log_file)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(log_level)

    _stop_listener()
    if use_queue:
        log_queue: Queue[logging.LogRecord] = Queue(maxsize=queue_capacity)
        root_logger.addHandler(NonBlockingQueueHandler(log_queue, sinks))
        with _listener_lock:
            _listener = QueueListener(
                log_queue,
                *sinks,
                respect_handler_level=True,
            )
            _listener.start()
    else:
        for sink in sinks:
            root_logger.addHandler(sink)

    for logger_name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = False
    access_logger.disabled = True


def get_dropped_log_counts() -> dict[str, int]:
    return dict(_dropped_logs)


def _build_sinks(
    formatter: logging.Formatter,
    *,
    stream: TextIO | None,
    log_file: str | None,
) -> list[logging.Handler]:
    console = logging.StreamHandler(stream or sys.stdout)
    console.setFormatter(formatter)
    sinks: list[logging.Handler] = [console]

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            path,
            when="midnight",
            backupCount=7,
            encoding="utf-8",
            utc=True,
        )
        file_handler.setFormatter(formatter)
        sinks.append(file_handler)
    return sinks


def _stop_listener() -> None:
    global _listener
    with _listener_lock:
        if _listener is not None:
            _listener.stop()
            _listener = None


atexit.register(_stop_listener)
