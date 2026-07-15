import asyncio
from datetime import UTC, datetime, timedelta

from long_invest.modules.providers.contracts import ProviderCapability, ProviderCode
from long_invest.modules.providers.resilience import (
    CircuitBreaker,
    CircuitState,
    InMemoryProviderRuntimeState,
    ProviderRateLimiter,
    ProviderRouteSetting,
)


def test_circuit_opens_after_three_failures_and_isolated_by_capability() -> None:
    now = datetime.now(UTC)
    circuit = CircuitBreaker()
    key = (ProviderCode.EASTMONEY, ProviderCapability.REALTIME_QUOTE_BATCH)
    other = (ProviderCode.EASTMONEY, ProviderCapability.HISTORICAL_DAILY_QFQ)
    for _ in range(3):
        circuit.record_failure(*key, now=now)
    assert circuit.state(*key, now=now) is CircuitState.OPEN
    assert circuit.state(*other, now=now) is CircuitState.CLOSED
    assert circuit.allow(*key, now=now + timedelta(seconds=59)) is False
    assert circuit.allow(*key, now=now + timedelta(seconds=60)) is True
    assert (
        circuit.state(*key, now=now + timedelta(seconds=60)) is CircuitState.HALF_OPEN
    )
    assert circuit.allow(*key, now=now + timedelta(seconds=60)) is False


def test_half_open_success_recovers_and_failures_use_cooldown_ladder() -> None:
    now = datetime.now(UTC)
    circuit = CircuitBreaker()
    key = (ProviderCode.EASTMONEY, ProviderCapability.REALTIME_QUOTE_BATCH)
    for _ in range(3):
        circuit.record_failure(*key, now=now)
    assert circuit.allow(*key, now=now + timedelta(seconds=60))
    circuit.record_failure(*key, now=now + timedelta(seconds=60))
    assert not circuit.allow(*key, now=now + timedelta(seconds=239))
    assert circuit.allow(*key, now=now + timedelta(seconds=240))
    circuit.record_success(*key, now=now + timedelta(seconds=240))
    assert circuit.state(*key, now=now + timedelta(seconds=240)) is CircuitState.CLOSED


def test_disabled_circuit_requires_single_probe_to_recover() -> None:
    now = datetime.now(UTC)
    circuit = CircuitBreaker()
    key = (ProviderCode.SINA, ProviderCapability.REALTIME_QUOTE_BATCH)
    circuit.disable(*key)
    assert not circuit.allow(*key, now=now)
    circuit.enable_for_probe(*key)
    assert circuit.allow(*key, now=now)
    assert not circuit.allow(*key, now=now)
    circuit.record_success(*key, now=now)
    assert circuit.state(*key, now=now) is CircuitState.CLOSED


def test_rate_limiter_reserves_realtime_capacity_and_degrades_conservatively() -> None:
    limiter = ProviderRateLimiter(
        global_limit=4, capability_limit=3, realtime_reserved=2
    )
    historical = ProviderCapability.HISTORICAL_DAILY_QFQ
    realtime = ProviderCapability.REALTIME_QUOTE_BATCH
    assert limiter.acquire(historical)
    assert limiter.acquire(historical)
    assert not limiter.acquire(historical)
    assert limiter.acquire(realtime)
    assert limiter.acquire(realtime)
    limiter.redis_failed()
    limiter.release(historical)
    limiter.release(historical)
    limiter.release(realtime)
    limiter.release(realtime)
    assert limiter.acquire(historical)
    assert not limiter.acquire(historical)


def test_async_runtime_enforces_token_rate_after_concurrency_release() -> None:
    now = [100.0]
    runtime = InMemoryProviderRuntimeState(
        global_limit=2, realtime_reserved=1, clock=lambda: now[0]
    )
    setting = ProviderRouteSetting(
        ProviderCode.EASTMONEY,
        ProviderCapability.HISTORICAL_DAILY_QFQ,
        rate_per_second=1,
    )

    async def scenario() -> None:
        assert await runtime.acquire(setting)
        await runtime.release(setting)
        assert not await runtime.acquire(setting)
        now[0] += 1
        assert await runtime.acquire(setting)

    asyncio.run(scenario())
