from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from time import monotonic

import structlog

from long_invest.modules.client_errors.contracts import (
    ClientErrorInput,
    ClientErrorReceipt,
)

logger = structlog.get_logger(__name__)
_SPACE_RE = re.compile(r"\s+")
_SECRET_RE = re.compile(
    r"(?i)(cookie|csrf|authorization|password|token|secret|api[-_ ]?key)"
    r"\s*[:=]\s*[^\s,;]+"
)
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")


@dataclass(slots=True)
class _FingerprintState:
    count: int


class ClientErrorCollector:
    def __init__(
        self,
        *,
        max_fingerprints: int = 1_000,
        max_samples_per_minute: int = 120,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if max_fingerprints < 1:
            raise ValueError("max_fingerprints must be positive")
        if max_samples_per_minute < 1:
            raise ValueError("max_samples_per_minute must be positive")
        self._max_fingerprints = max_fingerprints
        self._max_samples_per_minute = max_samples_per_minute
        self._clock = clock
        self._states: OrderedDict[str, _FingerprintState] = OrderedDict()
        self._sample_window_started = clock()
        self._sample_count = 0
        self._lock = Lock()

    def collect(self, value: ClientErrorInput) -> ClientErrorReceipt:
        message = _sanitize(value.message, limit=1_000)
        browser_summary = _sanitize(value.browser_summary, limit=300)
        fingerprint = _fingerprint(value, message)
        count, has_sample_capacity = self._increment(fingerprint)
        sampled = has_sample_capacity and (count == 1 or count & (count - 1) == 0)
        if sampled:
            logger.error(
                "frontend_error",
                category="frontend",
                message="前端异常上报",
                page_route=value.route,
                frontend_version=value.frontend_version,
                error_type=value.error_type,
                error_summary=message,
                browser_summary=browser_summary,
                client_request_id=value.request_id,
                occurred_at=value.occurred_at.isoformat(),
                fingerprint=fingerprint,
                occurrence_count=count,
            )
        return ClientErrorReceipt(fingerprint=fingerprint, sampled=sampled)

    def _increment(self, fingerprint: str) -> tuple[int, bool]:
        with self._lock:
            now = self._clock()
            if now - self._sample_window_started >= 60:
                self._sample_window_started = now
                self._sample_count = 0
            state = self._states.pop(fingerprint, None)
            if state is None:
                state = _FingerprintState(count=0)
            state.count += 1
            self._states[fingerprint] = state
            while len(self._states) > self._max_fingerprints:
                self._states.popitem(last=False)
            has_sample_capacity = self._sample_count < self._max_samples_per_minute
            if has_sample_capacity and (
                state.count == 1 or state.count & (state.count - 1) == 0
            ):
                self._sample_count += 1
            return state.count, has_sample_capacity


def _sanitize(value: str, *, limit: int) -> str:
    normalized = _SPACE_RE.sub(" ", value).strip()
    normalized = _SECRET_RE.sub(
        lambda match: f"{match.group(1)}=[REDACTED]", normalized
    )
    normalized = _LONG_TOKEN_RE.sub("[REDACTED]", normalized)
    return normalized[:limit]


def _fingerprint(value: ClientErrorInput, message: str) -> str:
    normalized_message = re.sub(r"\b\d+\b", "#", message.lower())
    source = "\x1f".join((value.route, value.error_type, normalized_message))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
