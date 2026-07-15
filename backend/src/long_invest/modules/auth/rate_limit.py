import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil
from typing import Protocol, runtime_checkable

from redis.exceptions import RedisError

_CHECK_SCRIPT = """
local now = tonumber(ARGV[1])
local cutoff = tonumber(ARGV[2])
local window = tonumber(ARGV[3])
local retry = 0
local blocked = false
for i = 1, 3 do
  redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', cutoff)
  local count = redis.call('ZCARD', KEYS[i])
  local limit = tonumber(ARGV[i + 3])
  if count >= limit then
    blocked = true
    local index = count - limit
    local item = redis.call('ZRANGE', KEYS[i], index, index, 'WITHSCORES')
    if #item == 2 then
      local wait = tonumber(item[2]) + window - now
      if wait > retry then retry = wait end
    end
  end
end
if blocked then return {0, math.max(1, math.ceil(retry / 1000))} end
for i = 1, 3 do
  redis.call('ZADD', KEYS[i], now, ARGV[7])
  redis.call('PEXPIRE', KEYS[i], window)
end
return {1, 0}
"""

_RECORD_SCRIPT = """
local cutoff = tonumber(ARGV[2])
for i = 1, 3 do
  redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', cutoff)
  redis.call('ZADD', KEYS[i], ARGV[1], ARGV[4])
  redis.call('PEXPIRE', KEYS[i], ARGV[3])
end
return 1
"""

_REMOVE_SCRIPT = """
for i = 1, 3 do
  redis.call('ZREM', KEYS[i], ARGV[1])
end
return 1
"""


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


class RedisEvalClient(Protocol):
    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> object: ...


class RedisLoginRateLimiter:
    def __init__(
        self,
        redis: RedisEvalClient,
        config: RateLimitConfig | None = None,
        *,
        namespace: str = "long-invest:auth:login",
    ) -> None:
        self._redis = redis
        self._config = config or RateLimitConfig()
        self._namespace = namespace

    async def check(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
    ) -> RateLimitDecision:
        keys = self._keys(ip, username)
        now_ms = _milliseconds(now)
        window_ms = int(self._config.window.total_seconds() * 1000)
        reservation_id = secrets.token_hex(16)
        check_task = asyncio.create_task(
            self._redis.eval(
                _CHECK_SCRIPT,
                3,
                *keys,
                now_ms,
                now_ms - window_ms,
                window_ms,
                self._config.per_ip,
                self._config.per_username,
                self._config.global_failures,
                reservation_id,
            )
        )
        try:
            raw = await asyncio.shield(check_task)
        except asyncio.CancelledError:
            await self._release_cancelled_reservation(
                check_task=check_task,
                keys=keys,
                reservation_id=reservation_id,
            )
            raise
        allowed, retry_after = raw  # type: ignore[misc]
        return RateLimitDecision(
            allowed=bool(int(allowed)),
            retry_after_seconds=None if int(allowed) else int(retry_after),
            reservation_id=reservation_id if int(allowed) else None,
        )

    async def _release_cancelled_reservation(
        self,
        *,
        check_task: asyncio.Task[object],
        keys: tuple[str, str, str],
        reservation_id: str,
    ) -> None:
        try:
            raw = await check_task
        except (RedisError, OSError):
            return
        allowed, _retry_after = raw  # type: ignore[misc]
        if int(allowed):
            cleanup_task = asyncio.create_task(
                self._redis.eval(
                    _REMOVE_SCRIPT,
                    3,
                    *keys,
                    reservation_id,
                )
            )
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task

    async def record_failure(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
        reservation_id: str | None = None,
    ) -> None:
        if reservation_id is not None:
            return
        keys = self._keys(ip, username)
        now_ms = _milliseconds(now)
        window_ms = int(self._config.window.total_seconds() * 1000)
        await self._redis.eval(
            _RECORD_SCRIPT,
            3,
            *keys,
            now_ms,
            now_ms - window_ms,
            window_ms,
            secrets.token_hex(16),
        )

    async def record_success(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
        reservation_id: str | None = None,
    ) -> None:
        if reservation_id is None:
            return
        await self._redis.eval(
            _REMOVE_SCRIPT,
            3,
            *self._keys(ip, username),
            reservation_id,
        )

    def _keys(self, ip: str, username: str) -> tuple[str, str, str]:
        ip_digest = hashlib.sha256(ip.encode()).hexdigest()
        username_digest = hashlib.sha256(username.casefold().encode()).hexdigest()
        return (
            f"{self._namespace}:ip:{ip_digest}",
            f"{self._namespace}:username:{username_digest}",
            f"{self._namespace}:global",
        )


class ResilientLoginRateLimiter:
    def __init__(
        self,
        primary: LoginRateLimitPolicy,
        fallback: LoginRateLimitPolicy,
        *,
        fallback_cooldown: timedelta = timedelta(seconds=30),
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._fallback_cooldown = fallback_cooldown
        self._fallback_until: datetime | None = None

    async def check(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
    ) -> RateLimitDecision:
        if self._using_fallback(now):
            return await self._fallback.check(ip=ip, username=username, now=now)
        try:
            return await self._primary.check(ip=ip, username=username, now=now)
        except (RedisError, OSError):
            self._activate_fallback(now)
            return await self._fallback.check(ip=ip, username=username, now=now)

    async def record_failure(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
        reservation_id: str | None = None,
    ) -> None:
        if self._using_fallback(now):
            await self._fallback.record_failure(
                ip=ip,
                username=username,
                now=now,
                reservation_id=reservation_id,
            )
            return
        try:
            await self._primary.record_failure(
                ip=ip,
                username=username,
                now=now,
                reservation_id=reservation_id,
            )
        except (RedisError, OSError):
            self._activate_fallback(now)
            await self._fallback.record_failure(
                ip=ip,
                username=username,
                now=now,
                reservation_id=reservation_id,
            )

    async def record_success(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
        reservation_id: str | None = None,
    ) -> None:
        try:
            await self._primary.record_success(
                ip=ip,
                username=username,
                now=now,
                reservation_id=reservation_id,
            )
        except (RedisError, OSError):
            self._activate_fallback(now)
        await self._fallback.record_success(
            ip=ip,
            username=username,
            now=now,
            reservation_id=reservation_id,
        )

    def _using_fallback(self, now: datetime) -> bool:
        return self._fallback_until is not None and now < self._fallback_until

    def _activate_fallback(self, now: datetime) -> None:
        self._fallback_until = now + self._fallback_cooldown


def _milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)
