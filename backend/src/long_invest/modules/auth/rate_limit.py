from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int | None = None


@dataclass(frozen=True)
class RateLimitConfig:
    per_ip: int = 5
    per_username: int = 5
    global_failures: int = 20
    window: timedelta = timedelta(minutes=15)


@runtime_checkable
class LoginRateLimitPolicy(Protocol):
    def check(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
    ) -> RateLimitDecision: ...

    def record_failure(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
    ) -> None: ...

    def record_success(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
    ) -> None: ...


class InMemoryLoginRateLimiter:
    """Conservative process-local fallback when shared limiting is unavailable."""

    def __init__(self, config: RateLimitConfig | None = None) -> None:
        self._config = config or RateLimitConfig()
        self._failures: list[tuple[datetime, str, str]] = []

    def check(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
    ) -> RateLimitDecision:
        self._prune(now)
        ip_failures = [item for item in self._failures if item[1] == ip]
        username_failures = [item for item in self._failures if item[2] == username]
        blocked = (
            len(ip_failures) >= self._config.per_ip
            or len(username_failures) >= self._config.per_username
            or len(self._failures) >= self._config.global_failures
        )
        if not blocked:
            return RateLimitDecision(allowed=True)
        oldest = min(item[0] for item in self._failures)
        retry_after = max(
            1,
            ceil((oldest + self._config.window - now).total_seconds()),
        )
        return RateLimitDecision(
            allowed=False,
            retry_after_seconds=retry_after,
        )

    def record_failure(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
    ) -> None:
        self._prune(now)
        self._failures.append((now, ip, username))

    def record_success(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
    ) -> None:
        self._prune(now)

    def _prune(self, now: datetime) -> None:
        cutoff = now - self._config.window
        self._failures = [item for item in self._failures if item[0] > cutoff]
