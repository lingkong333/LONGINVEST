from datetime import UTC, datetime, timedelta

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from long_invest.modules.auth.rate_limit import (
    InMemoryLoginRateLimiter,
    LoginRateLimitPolicy,
    RateLimitConfig,
    RedisLoginRateLimiter,
    ResilientLoginRateLimiter,
)

NOW = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class ScriptedRedis:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[object, ...]] = []

    async def eval(self, *args: object) -> object:
        self.calls.append(args)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class UnavailableLimiter:
    async def check(self, **kwargs: object):  # type: ignore[no-untyped-def]
        raise RedisConnectionError("redis unavailable")

    async def record_failure(self, **kwargs: object) -> None:
        raise RedisConnectionError("redis unavailable")

    async def record_success(self, **kwargs: object) -> None:
        raise RedisConnectionError("redis unavailable")


class ImmediatelyRecoveringLimiter:
    def __init__(self) -> None:
        self.check_calls = 0

    async def check(self, **kwargs: object):  # type: ignore[no-untyped-def]
        self.check_calls += 1
        return type("Decision", (), {"allowed": True, "retry_after_seconds": None})()

    async def record_failure(self, **kwargs: object) -> None:
        raise RedisConnectionError("transient redis failure")

    async def record_success(self, **kwargs: object) -> None:
        return None


def test_local_limiter_implements_the_login_policy_interface() -> None:
    limiter = InMemoryLoginRateLimiter()

    assert isinstance(limiter, LoginRateLimitPolicy)


@pytest.mark.anyio
async def test_limiter_checks_ip_username_and_global_failure_dimensions() -> None:
    ip_limiter = InMemoryLoginRateLimiter(
        RateLimitConfig(per_ip=2, per_username=10, global_failures=10)
    )
    for username in ("first", "second"):
        await ip_limiter.record_failure(
            ip="203.0.113.1",
            username=username,
            now=NOW,
        )
    assert (
        await ip_limiter.check(ip="203.0.113.1", username="third", now=NOW)
    ).allowed is False

    username_limiter = InMemoryLoginRateLimiter(
        RateLimitConfig(per_ip=10, per_username=2, global_failures=10)
    )
    for ip in ("203.0.113.1", "203.0.113.2"):
        await username_limiter.record_failure(ip=ip, username="admin", now=NOW)
    assert (
        await username_limiter.check(
            ip="203.0.113.3",
            username="admin",
            now=NOW,
        )
    ).allowed is False

    global_limiter = InMemoryLoginRateLimiter(
        RateLimitConfig(per_ip=10, per_username=10, global_failures=2)
    )
    await global_limiter.record_failure(
        ip="203.0.113.1",
        username="first",
        now=NOW,
    )
    await global_limiter.record_failure(
        ip="203.0.113.2",
        username="second",
        now=NOW,
    )
    assert (
        await global_limiter.check(
            ip="203.0.113.3",
            username="third",
            now=NOW,
        )
    ).allowed is False


@pytest.mark.anyio
async def test_limiter_recovers_after_its_rolling_window() -> None:
    limiter = InMemoryLoginRateLimiter(
        RateLimitConfig(
            per_ip=1,
            per_username=1,
            global_failures=1,
            window=timedelta(minutes=1),
        )
    )
    await limiter.record_failure(ip="203.0.113.1", username="admin", now=NOW)

    blocked = await limiter.check(ip="203.0.113.1", username="admin", now=NOW)
    recovered = await limiter.check(
        ip="203.0.113.1",
        username="admin",
        now=NOW + timedelta(minutes=1),
    )

    assert blocked.allowed is False
    assert blocked.retry_after_seconds == 60
    assert recovered.allowed is True


@pytest.mark.anyio
async def test_redis_limiter_uses_hashed_shared_keys_for_three_dimensions() -> None:
    redis = ScriptedRedis([0, 42], 1)
    limiter = RedisLoginRateLimiter(redis, RateLimitConfig())

    decision = await limiter.check(ip="203.0.113.1", username="admin", now=NOW)
    await limiter.record_failure(ip="203.0.113.1", username="admin", now=NOW)

    assert decision.allowed is False
    assert decision.retry_after_seconds == 42
    assert all(call[1] == 3 for call in redis.calls)
    assert "203.0.113.1" not in repr(redis.calls)
    assert "admin" not in repr(redis.calls)


@pytest.mark.anyio
async def test_redis_failure_automatically_uses_conservative_local_limiter() -> None:
    local = InMemoryLoginRateLimiter(
        RateLimitConfig(per_ip=1, per_username=1, global_failures=1)
    )
    limiter = ResilientLoginRateLimiter(UnavailableLimiter(), local)

    await limiter.record_failure(ip="203.0.113.1", username="admin", now=NOW)
    decision = await limiter.check(ip="203.0.113.1", username="admin", now=NOW)

    assert decision.allowed is False
    assert decision.retry_after_seconds == 900


@pytest.mark.anyio
async def test_fallback_remains_active_during_a_conservative_cooldown() -> None:
    primary = ImmediatelyRecoveringLimiter()
    local = InMemoryLoginRateLimiter(
        RateLimitConfig(per_ip=1, per_username=1, global_failures=1)
    )
    limiter = ResilientLoginRateLimiter(primary, local)

    await limiter.record_failure(ip="203.0.113.1", username="admin", now=NOW)
    decision = await limiter.check(ip="203.0.113.1", username="admin", now=NOW)

    assert decision.allowed is False
    assert primary.check_calls == 0
