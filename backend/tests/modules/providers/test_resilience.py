import asyncio
from datetime import UTC, datetime, timedelta

from long_invest.modules.providers.contracts import (
    ProviderBatchResult,
    ProviderCapability,
    ProviderCode,
)
from long_invest.modules.providers.resilience import (
    CircuitBreaker,
    CircuitState,
    InMemoryProviderRuntimeState,
    ProviderInvocationPipeline,
    ProviderRateLimiter,
    ProviderRouteSetting,
    RedisProviderRuntimeState,
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
        lease = await runtime.acquire(setting)
        assert lease is not None
        await runtime.release(setting, lease)
        assert not await runtime.acquire(setting)
        now[0] += 1
        assert await runtime.acquire(setting)

    asyncio.run(scenario())


def test_fractional_rate_allows_one_request_every_five_seconds() -> None:
    now = [100.0]
    runtime = InMemoryProviderRuntimeState(clock=lambda: now[0])
    setting = ProviderRouteSetting(
        ProviderCode.EASTMONEY,
        ProviderCapability.HISTORICAL_DAILY_QFQ,
        rate_per_second=0.2,
    )

    async def scenario() -> None:
        lease = await runtime.acquire(setting)
        assert lease is not None
        await runtime.release(setting, lease)
        now[0] += 4.9
        assert await runtime.acquire(setting) is None
        now[0] += 0.1
        assert await runtime.acquire(setting) is not None

    asyncio.run(scenario())


def test_redis_lease_is_released_by_the_backend_that_acquired_it() -> None:
    class Redis:
        def __init__(self) -> None:
            self.failed = False
            self.eval_calls = 0

        async def eval(self, *args):
            del args
            self.eval_calls += 1
            if self.failed:
                raise ConnectionError("redis unavailable")
            return 1

    redis = Redis()
    runtime = RedisProviderRuntimeState(redis, realtime_reserved=0)
    setting = ProviderRouteSetting(
        ProviderCode.EASTMONEY,
        ProviderCapability.REALTIME_QUOTE_BATCH,
        rate_per_second=100,
    )

    async def scenario() -> None:
        redis_lease = await runtime.acquire(setting)
        assert redis_lease is not None and redis_lease.backend == "redis"
        redis.failed = True
        await runtime.release(setting, redis_lease)
        local_lease = await runtime.acquire(setting)
        assert local_lease is not None and local_lease.backend == "local"
        redis.failed = False
        calls = redis.eval_calls
        await runtime.release(setting, local_lease)
        assert redis.eval_calls == calls

    asyncio.run(scenario())


def test_repeated_manual_half_open_still_allows_only_one_probe() -> None:
    runtime = InMemoryProviderRuntimeState()
    setting = ProviderRouteSetting(
        ProviderCode.EASTMONEY,
        ProviderCapability.REALTIME_QUOTE_BATCH,
    )

    async def scenario() -> None:
        await runtime.force_half_open(setting)
        assert await runtime.allow(setting, probe=True)
        await runtime.force_half_open(setting)
        assert not await runtime.allow(setting, probe=True)

    asyncio.run(scenario())


def test_pipeline_observer_receives_persistable_health_and_circuit_snapshots() -> None:
    runtime = InMemoryProviderRuntimeState()
    calls = []

    class Observer:
        async def record_outcome(self, setting, **kwargs):
            calls.append((setting, kwargs))

    setting = ProviderRouteSetting(
        ProviderCode.EASTMONEY,
        ProviderCapability.REALTIME_QUOTE_BATCH,
        rate_per_second=100,
    )
    pipeline = ProviderInvocationPipeline(runtime, Observer())

    async def scenario() -> None:
        for _ in range(3):
            await pipeline.call(
                setting,
                lambda: _batch_failure(),
                deadline=datetime.now(UTC) + timedelta(seconds=1),
            )

    async def _batch_failure():
        return ProviderBatchResult(batch_error_code="PROVIDER_FAILED")

    asyncio.run(scenario())
    assert len(calls) == 3
    assert calls[-1][1]["success"] is False
    assert calls[-1][1]["snapshot"]["state"] == "OPEN"
    assert calls[-1][1]["error_code"] == "PROVIDER_FAILED"


def test_observer_failure_does_not_count_as_upstream_failure() -> None:
    runtime = InMemoryProviderRuntimeState()

    class FailingObserver:
        async def record_outcome(self, setting, **kwargs):
            del setting, kwargs
            raise RuntimeError("database unavailable")

    setting = ProviderRouteSetting(
        ProviderCode.EASTMONEY,
        ProviderCapability.REALTIME_QUOTE_BATCH,
        rate_per_second=100,
    )
    pipeline = ProviderInvocationPipeline(runtime, FailingObserver())

    async def scenario() -> None:
        try:
            await pipeline.call(
                setting,
                lambda: successful_result(),
                deadline=datetime.now(UTC) + timedelta(seconds=1),
            )
        except RuntimeError as error:
            assert str(error) == "database unavailable"
        else:
            raise AssertionError("observer failure should remain visible")
        snapshot = await runtime.circuit_snapshot(setting)
        assert snapshot.get("failures", 0) == 0
        assert snapshot["state"] == "CLOSED"

    async def successful_result() -> ProviderBatchResult:
        return ProviderBatchResult()

    asyncio.run(scenario())
