import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from long_invest.modules.auth.rate_limit import (
    InMemoryLoginRateLimiter,
    LoginRateLimitPolicy,
    RateLimitConfig,
)

NOW = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


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
async def test_check_atomically_reserves_capacity_before_password_work() -> None:
    limiter = InMemoryLoginRateLimiter(
        RateLimitConfig(per_ip=1, per_username=1, global_failures=1)
    )

    first, second = await asyncio.gather(
        limiter.check(ip="203.0.113.1", username="admin", now=NOW),
        limiter.check(ip="203.0.113.1", username="admin", now=NOW),
    )

    assert [first.allowed, second.allowed].count(True) == 1
    assert [first.allowed, second.allowed].count(False) == 1
    allowed = first if first.allowed else second
    await limiter.record_success(
        ip="203.0.113.1",
        username="admin",
        now=NOW,
        reservation_id=allowed.reservation_id,
    )
    assert (await limiter.check(ip="203.0.113.1", username="admin", now=NOW)).allowed
