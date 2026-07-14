from datetime import UTC, datetime, timedelta

from long_invest.modules.auth.rate_limit import (
    InMemoryLoginRateLimiter,
    LoginRateLimitPolicy,
    RateLimitConfig,
)

NOW = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)


def test_local_limiter_implements_the_login_policy_interface() -> None:
    limiter = InMemoryLoginRateLimiter()

    assert isinstance(limiter, LoginRateLimitPolicy)


def test_limiter_checks_ip_username_and_global_failure_dimensions() -> None:
    ip_limiter = InMemoryLoginRateLimiter(
        RateLimitConfig(per_ip=2, per_username=10, global_failures=10)
    )
    for username in ("first", "second"):
        ip_limiter.record_failure(ip="203.0.113.1", username=username, now=NOW)
    assert (
        ip_limiter.check(ip="203.0.113.1", username="third", now=NOW).allowed is False
    )

    username_limiter = InMemoryLoginRateLimiter(
        RateLimitConfig(per_ip=10, per_username=2, global_failures=10)
    )
    for ip in ("203.0.113.1", "203.0.113.2"):
        username_limiter.record_failure(ip=ip, username="admin", now=NOW)
    assert (
        username_limiter.check(ip="203.0.113.3", username="admin", now=NOW).allowed
        is False
    )

    global_limiter = InMemoryLoginRateLimiter(
        RateLimitConfig(per_ip=10, per_username=10, global_failures=2)
    )
    global_limiter.record_failure(ip="203.0.113.1", username="first", now=NOW)
    global_limiter.record_failure(ip="203.0.113.2", username="second", now=NOW)
    assert (
        global_limiter.check(ip="203.0.113.3", username="third", now=NOW).allowed
        is False
    )


def test_limiter_recovers_after_its_rolling_window() -> None:
    limiter = InMemoryLoginRateLimiter(
        RateLimitConfig(
            per_ip=1,
            per_username=1,
            global_failures=1,
            window=timedelta(minutes=1),
        )
    )
    limiter.record_failure(ip="203.0.113.1", username="admin", now=NOW)

    blocked = limiter.check(ip="203.0.113.1", username="admin", now=NOW)
    recovered = limiter.check(
        ip="203.0.113.1",
        username="admin",
        now=NOW + timedelta(minutes=1),
    )

    assert blocked.allowed is False
    assert blocked.retry_after_seconds == 60
    assert recovered.allowed is True
