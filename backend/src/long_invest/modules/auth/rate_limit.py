import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int | None = None
    reservation_id: str | None = None


@dataclass(frozen=True)
class RateLimitConfig:
    per_ip: int = 5
    per_username: int = 5
    global_failures: int = 20
    window: timedelta = timedelta(minutes=15)


@runtime_checkable
class LoginRateLimitPolicy(Protocol):
    async def check(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
    ) -> RateLimitDecision: ...

    async def record_failure(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
        reservation_id: str | None = None,
    ) -> None: ...

    async def record_success(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
        reservation_id: str | None = None,
    ) -> None: ...


class InMemoryLoginRateLimiter:
    """Conservative process-local fallback when shared limiting is unavailable."""

    def __init__(self, config: RateLimitConfig | None = None) -> None:
        self._config = config or RateLimitConfig()
        self._failures: list[tuple[datetime, str, str, str]] = []

    async def check(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
    ) -> RateLimitDecision:
        self._prune(now)
        dimensions = (
            (
                [item for item in self._failures if item[1] == ip],
                self._config.per_ip,
            ),
            (
                [item for item in self._failures if item[2] == username],
                self._config.per_username,
            ),
            (self._failures, self._config.global_failures),
        )
        blocked_dimensions = [
            (failures, limit)
            for failures, limit in dimensions
            if len(failures) >= limit
        ]
        if not blocked_dimensions:
            reservation_id = secrets.token_hex(16)
            self._failures.append((now, ip, username, reservation_id))
            return RateLimitDecision(
                allowed=True,
                reservation_id=reservation_id,
            )
        retry_after = max(
            ceil(
                (
                    failures[len(failures) - limit][0] + self._config.window - now
                ).total_seconds()
            )
            for failures, limit in blocked_dimensions
        )
        return RateLimitDecision(
            allowed=False,
            retry_after_seconds=max(1, retry_after),
        )

    async def record_failure(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
        reservation_id: str | None = None,
    ) -> None:
        self._prune(now)
        if reservation_id is not None and any(
            item[3] == reservation_id for item in self._failures
        ):
            return
        self._failures.append(
            (now, ip, username, reservation_id or secrets.token_hex(16))
        )

    async def record_success(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
        reservation_id: str | None = None,
    ) -> None:
        self._prune(now)
        if reservation_id is not None:
            self._failures = [
                item for item in self._failures if item[3] != reservation_id
            ]

    def _prune(self, now: datetime) -> None:
        cutoff = now - self._config.window
        self._failures = [item for item in self._failures if item[0] > cutoff]
