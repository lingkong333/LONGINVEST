import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import wraps
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.auth.audit import AuditContext
from long_invest.modules.providers.contracts import (
    ProbeResult,
    ProviderBatchResult,
    ProviderCapability,
    ProviderCode,
    RealtimeQuote,
)
from long_invest.modules.providers.models import (
    ProviderCircuitState,
    ProviderHealthState,
    ProviderMutationRequest,
)
from long_invest.modules.providers.repository import ProviderRepository
from long_invest.modules.providers.resilience import (
    ProviderInvocationPipeline,
    ProviderRouteSetting,
    RedisProviderRuntimeState,
)
from long_invest.modules.providers.service import ProviderService
from long_invest.platform.errors import AppError


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


def audit_context(key: str = "idem-1") -> AuditContext:
    return AuditContext(
        request_id="request-1",
        idempotency_key=key,
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
    )


class Transaction:
    def __init__(self, session) -> None:
        self.session = session

    async def __aenter__(self):
        self.session.in_transaction = True

    async def __aexit__(self, kind, value, traceback):
        self.session.in_transaction = False
        if kind is not None:
            self.session.rolled_back = True
            self.session.added.clear()


class FakeSession:
    def __init__(self, scalar_results=None) -> None:
        self.scalar_results = list(scalar_results or [])
        self.added = []
        self.rolled_back = False
        self.in_transaction = False

    def begin(self):
        return Transaction(self)

    async def scalar(self, statement):
        del statement
        return self.scalar_results.pop(0) if self.scalar_results else None

    async def execute(self, statement, parameters=None):
        del statement
        self.last_execute_parameters = parameters

    def add(self, item):
        assert self.in_transaction
        self.added.append(item)

    async def flush(self):
        assert self.in_transaction


class FailingAudit:
    async def record(self, **kwargs):
        del kwargs
        raise RuntimeError("audit unavailable")


class RecordingAudit:
    def __init__(self) -> None:
        self.calls = []

    async def record(self, **kwargs):
        self.calls.append(kwargs)


class RecordingEvents:
    def __init__(self) -> None:
        self.calls = []

    async def append(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def test_repository_fails_closed_without_audit_or_outbox() -> None:
    with pytest.raises(ValueError):
        ProviderRepository(FakeSession(), audit=None, events=RecordingEvents())
    with pytest.raises(ValueError):
        ProviderRepository(FakeSession(), audit=RecordingAudit(), events=None)


@async_test
async def test_read_boundary_closes_real_async_session_transaction() -> None:
    session = AsyncSession()
    repository = ProviderRepository(
        session, audit=RecordingAudit(), events=RecordingEvents()
    )
    repository._idempotent_replay = AsyncMock(return_value=None)
    assert await repository.replay_mutation("idem", "digest") is None
    assert session.in_transaction() is False
    async with session.begin():
        assert session.in_transaction() is True
    await session.close()


@async_test
async def test_settings_audit_failure_rolls_back_config_history_and_outbox() -> None:
    session = FakeSession([None, None])
    events = RecordingEvents()
    repository = ProviderRepository(session, audit=FailingAudit(), events=events)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await repository.mutate_settings(
            ProviderCode.EASTMONEY,
            frozenset({ProviderCapability.REALTIME_QUOTE_BATCH}),
            {"enabled": False},
            expected_version=0,
            reason="maintenance",
            context=audit_context(),
        )
    assert session.rolled_back is True
    assert session.added == []
    assert events.calls == []


@async_test
async def test_same_idempotency_key_with_different_digest_is_conflict() -> None:
    existing = ProviderMutationRequest(
        idempotency_key="idem-1",
        request_digest="old-digest",
        operation="settings",
        object_id="EASTMONEY",
        response_summary={"version": 2},
        request_id="request-1",
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
    )
    repository = ProviderRepository(
        FakeSession([existing]), audit=RecordingAudit(), events=RecordingEvents()
    )
    with pytest.raises(AppError) as caught:
        await repository._idempotent_replay("idem-1", "new-digest")
    assert caught.value.code == "IDEMPOTENCY_KEY_CONFLICT"
    assert caught.value.status_code == 409


@async_test
async def test_settings_persists_history_audit_outbox_atomically() -> None:
    session = FakeSession([None, None])
    audit = RecordingAudit()
    events = RecordingEvents()
    repository = ProviderRepository(session, audit=audit, events=events)
    result = await repository.mutate_settings(
        ProviderCode.EASTMONEY,
        frozenset({ProviderCapability.REALTIME_QUOTE_BATCH}),
        {"enabled": False},
        expected_version=0,
        reason="maintenance",
        context=audit_context(),
    )
    assert result["version"] == 1
    assert result["capabilities"][0]["enabled"] is False
    assert {item.__tablename__ for item in session.added} == {
        "provider_config_version",
        "provider_capability_setting",
        "provider_mutation_request",
    }
    assert audit.calls[0]["context"].trusted_ip == "127.0.0.1"
    assert events.calls[0][1]["idempotency_key"] == "idem-1"
    assert session.last_execute_parameters == {
        "provider_key": "provider-config:EASTMONEY"
    }


@async_test
async def test_runtime_outcome_persists_health_circuit_history_and_events() -> None:
    session = FakeSession([None, None])
    events = RecordingEvents()
    repository = ProviderRepository(session, audit=RecordingAudit(), events=events)
    setting = ProviderRouteSetting(
        ProviderCode.EASTMONEY,
        ProviderCapability.REALTIME_QUOTE_BATCH,
    )
    await repository.record_outcome(
        setting,
        success=False,
        snapshot={
            "state": "OPEN",
            "consecutive_failures": 3,
            "cooldown_index": 0,
            "opened_at": datetime.now(UTC),
        },
        occurred_at=datetime.now(UTC),
        error_code="PROVIDER_FAILED",
    )
    assert {item.__tablename__ for item in session.added} == {
        "provider_health_state",
        "provider_circuit_state",
        "provider_circuit_history",
    }
    assert [call[0][0] for call in events.calls] == [
        "provider.circuit_state_changed",
        "provider.circuit_opened",
        "provider.request_failed",
    ]


@async_test
async def test_schema_anomaly_persists_sample_alarm_and_health_metrics() -> None:
    session = FakeSession([None, None])
    events = RecordingEvents()
    repository = ProviderRepository(session, audit=RecordingAudit(), events=events)
    setting = ProviderRouteSetting(
        ProviderCode.EASTMONEY,
        ProviderCapability.REALTIME_QUOTE_BATCH,
    )
    occurred_at = datetime.now(UTC)
    await repository.record_outcome(
        setting,
        success=False,
        snapshot={
            "state": "OPEN",
            "consecutive_failures": 3,
            "cooldown_index": 0,
            "opened_at": occurred_at,
        },
        occurred_at=occurred_at,
        error_code="PROVIDER_SCHEMA_INCOMPATIBLE",
        latency_ms=37,
        switched=True,
        response_sample={
            "body_excerpt": "bad upstream response",
            "authorization": "secret-value",
        },
    )
    health = next(
        item for item in session.added if item.__tablename__ == "provider_health_state"
    )
    sample = next(
        item
        for item in session.added
        if item.__tablename__ == "provider_failure_sample"
    )
    assert sample.expires_at == occurred_at + timedelta(days=7)
    assert sample.sample["body_excerpt"] == "bad upstream response"
    assert sample.sample["authorization"] == "[REDACTED]"
    assert health.metrics["success_rate"] == 0
    assert health.metrics["p95_latency_ms"] == 37
    assert health.metrics["switch_count"] == 1
    assert health.metrics["schema_error_count"] == 1
    assert health.metrics["cooldown_remaining_seconds"] == 60
    schema_event = next(
        call for call in events.calls if call[0][0] == "provider.schema_changed"
    )
    assert schema_event[0][1]["alert_code"] == "PROVIDER_SCHEMA_CHANGED"


@pytest.mark.parametrize(
    ("previous_state", "state", "success", "expected_event"),
    [
        ("CLOSED", "OPEN", False, "provider.circuit_opened"),
        ("OPEN", "HALF_OPEN", False, "provider.half_opened"),
        ("OPEN", "CLOSED", True, "provider.recovered"),
    ],
)
@async_test
async def test_circuit_transition_publishes_public_contract_event(
    previous_state: str,
    state: str,
    success: bool,
    expected_event: str,
) -> None:
    occurred_at = datetime.now(UTC)
    health = ProviderHealthState(
        provider_code="EASTMONEY",
        capability="REALTIME_QUOTE_BATCH",
        status="HEALTHY",
        consecutive_failures=0,
        metrics={},
    )
    circuit = ProviderCircuitState(
        provider_code="EASTMONEY",
        capability="REALTIME_QUOTE_BATCH",
        state=previous_state,
        consecutive_failures=0,
        cooldown_index=0,
        opened_at=occurred_at,
    )
    events = RecordingEvents()
    repository = ProviderRepository(
        FakeSession([health, circuit]),
        audit=RecordingAudit(),
        events=events,
    )
    await repository.record_outcome(
        ProviderRouteSetting(
            ProviderCode.EASTMONEY,
            ProviderCapability.REALTIME_QUOTE_BATCH,
        ),
        success=success,
        snapshot={
            "state": state,
            "consecutive_failures": 0 if success else 3,
            "cooldown_index": 0,
            "opened_at": None if state == "CLOSED" else occurred_at,
        },
        occurred_at=occurred_at,
        error_code=None if success else "PROVIDER_FAILED",
    )
    public_event = next(call for call in events.calls if call[0][0] == expected_event)
    assert public_event[1]["idempotency_key"].endswith(
        f":transition:{previous_state}:{state}"
    )


@async_test
async def test_degraded_and_actual_auto_switch_publish_public_events() -> None:
    occurred_at = datetime.now(UTC)
    health = ProviderHealthState(
        provider_code="SINA",
        capability="REALTIME_QUOTE_BATCH",
        status="HEALTHY",
        consecutive_failures=0,
        metrics={},
    )
    circuit = ProviderCircuitState(
        provider_code="SINA",
        capability="REALTIME_QUOTE_BATCH",
        state="CLOSED",
        consecutive_failures=0,
        cooldown_index=0,
        opened_at=None,
    )
    events = RecordingEvents()
    repository = ProviderRepository(
        FakeSession([health, circuit]),
        audit=RecordingAudit(),
        events=events,
    )
    await repository.record_outcome(
        ProviderRouteSetting(
            ProviderCode.SINA,
            ProviderCapability.REALTIME_QUOTE_BATCH,
        ),
        success=False,
        snapshot={"state": "CLOSED", "consecutive_failures": 1},
        occurred_at=occurred_at,
        error_code="PROVIDER_ITEM_MISSING",
        switched=True,
    )
    event_names = [call[0][0] for call in events.calls]
    assert "provider.degraded" in event_names
    assert "provider.auto_switched" in event_names
    keys = [
        call[1]["idempotency_key"]
        for call in events.calls
        if call[0][0] in {"provider.degraded", "provider.auto_switched"}
    ]
    assert len(keys) == len(set(keys))


@async_test
async def test_redis_half_open_probe_is_persisted_before_upstream_recovers() -> None:
    class HalfOpenRedis:
        def __init__(self) -> None:
            self.raw = json.dumps(
                {
                    "state": "OPEN",
                    "failures": 3,
                    "level": 0,
                    "opened_at": 0,
                }
            )

        async def eval(self, script, key_count, *args):
            del key_count
            if "state['state']='HALF_OPEN'" in script:
                state = json.loads(self.raw)
                state["state"] = "HALF_OPEN"
                state["probe_token"] = "probe-token-1"
                self.raw = json.dumps(state)
                return 1
            if "local total=" in script:
                return 1
            if "redis.call('set',KEYS[1],ARGV[1])" in script:
                self.raw = args[-1]
                return 1
            return 1

        async def get(self, key):
            del key
            return self.raw

    occurred_at = datetime.now(UTC)
    health = ProviderHealthState(
        provider_code="EASTMONEY",
        capability="REALTIME_QUOTE_BATCH",
        status="CIRCUIT_OPEN",
        consecutive_failures=3,
        metrics={},
    )
    circuit = ProviderCircuitState(
        provider_code="EASTMONEY",
        capability="REALTIME_QUOTE_BATCH",
        state="OPEN",
        consecutive_failures=3,
        cooldown_index=0,
        opened_at=occurred_at,
    )
    events = RecordingEvents()
    repository = ProviderRepository(
        FakeSession([health, circuit, health, circuit]),
        audit=RecordingAudit(),
        events=events,
    )
    setting = ProviderRouteSetting(
        ProviderCode.EASTMONEY,
        ProviderCapability.REALTIME_QUOTE_BATCH,
        rate_per_second=100,
    )
    pipeline = ProviderInvocationPipeline(
        RedisProviderRuntimeState(HalfOpenRedis()), repository
    )

    async def operation() -> ProviderBatchResult:
        assert circuit.state == "HALF_OPEN"
        return ProviderBatchResult()

    await pipeline.call(
        setting,
        operation,
        deadline=datetime.now(UTC) + timedelta(seconds=1),
    )
    event_names = [call[0][0] for call in events.calls]
    assert event_names.index("provider.half_opened") < event_names.index(
        "provider.recovered"
    )
    half_opened = next(
        call for call in events.calls if call[0][0] == "provider.half_opened"
    )
    assert half_opened[1]["idempotency_key"].endswith(":probe-token-1")


@async_test
async def test_same_idempotent_request_replays_persisted_summary() -> None:
    existing = ProviderMutationRequest(
        idempotency_key="idem-1",
        request_digest="same",
        operation="settings",
        object_id="EASTMONEY",
        response_summary={"version": 2},
        request_id="request-1",
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
    )
    repository = ProviderRepository(
        FakeSession([existing]), audit=RecordingAudit(), events=RecordingEvents()
    )
    assert await repository._idempotent_replay("idem-1", "same") == {"version": 2}


def quote(symbol: str, source: ProviderCode, price: str) -> RealtimeQuote:
    now = datetime.now(UTC)
    value = Decimal(price)
    return RealtimeQuote(
        symbol,
        value,
        Decimal("10"),
        Decimal("11"),
        Decimal("9"),
        Decimal("9.8"),
        100,
        Decimal("1000"),
        now,
        now,
        source,
    )


class DiagnosticProvider:
    def __init__(self, code: ProviderCode, price: str) -> None:
        self.code = code
        self.price = price
        self.capabilities = frozenset({ProviderCapability.REALTIME_QUOTE_BATCH})

    async def realtime_quotes(self, symbols, deadline):
        del deadline
        return ProviderBatchResult(
            tuple(quote(symbol, self.code, self.price) for symbol in symbols)
        )


class ServiceRepository:
    def __init__(self) -> None:
        self.diagnostic = None
        self.persisted_probe = None
        self.circuit_id = uuid4()
        self.replay = None

    async def replay_mutation(self, idempotency_key, digest):
        del idempotency_key, digest
        return self.replay

    async def audit_diagnostic(self, symbols, summary, *, reason, context):
        self.diagnostic = (symbols, summary, reason, context)

    async def circuit(self, circuit_id):
        assert circuit_id == self.circuit_id
        return {
            "provider_code": "EASTMONEY",
            "capability": "REALTIME_QUOTE_BATCH",
        }

    async def persist_probe(self, circuit_id, result, **kwargs):
        self.persisted_probe = (circuit_id, result, kwargs)
        return {"id": str(circuit_id), **result}


class ProbeRouter:
    def __init__(self, providers=None) -> None:
        self.calls = []
        self.providers = providers or {}

    async def probe(self, setting, deadline, *, force_half_open=False):
        self.calls.append((setting, deadline, force_half_open))
        return ProbeResult(
            setting.provider,
            setting.capability,
            True,
            datetime.now(UTC),
            1,
        )

    async def diagnostic_quotes(self, provider_code, symbols, deadline):
        return await self.providers[provider_code].realtime_quotes(symbols, deadline)


class ProbeRuntime:
    async def circuit_snapshot(self, setting):
        del setting
        return {
            "state": "CLOSED",
            "consecutive_failures": 0,
            "cooldown_index": 0,
            "opened_at": None,
        }


@async_test
async def test_probe_uses_real_router_and_persists_result_with_audit_context() -> None:
    repository = ServiceRepository()
    router = ProbeRouter()
    provider = DiagnosticProvider(ProviderCode.EASTMONEY, "10")
    service = ProviderService(
        router,
        {ProviderCode.EASTMONEY: provider},
        repository,
        ProbeRuntime(),
    )
    result = await service.reset_circuit(
        repository.circuit_id,
        reason="operator reset",
        audit_context=audit_context(),
    )
    assert len(router.calls) == 1
    assert result["state"] == "CLOSED"
    assert repository.persisted_probe[2]["action_code"] == (
        "provider.circuit_reset_probed"
    )
    assert router.calls[0][2] is True


@async_test
async def test_normal_probe_does_not_force_half_open() -> None:
    repository = ServiceRepository()
    router = ProbeRouter()
    provider = DiagnosticProvider(ProviderCode.EASTMONEY, "10")
    service = ProviderService(
        router,
        {ProviderCode.EASTMONEY: provider},
        repository,
        ProbeRuntime(),
    )
    await service.probe_circuit(
        repository.circuit_id,
        reason="operator probe",
        audit_context=audit_context(),
    )
    assert router.calls[0][2] is False


@async_test
async def test_replayed_reset_does_not_repeat_external_probe() -> None:
    repository = ServiceRepository()
    repository.replay = {"id": str(repository.circuit_id), "state": "CLOSED"}
    router = ProbeRouter()
    provider = DiagnosticProvider(ProviderCode.EASTMONEY, "10")
    service = ProviderService(
        router,
        {ProviderCode.EASTMONEY: provider},
        repository,
        ProbeRuntime(),
    )
    result = await service.reset_circuit(
        repository.circuit_id,
        reason="operator reset",
        audit_context=audit_context(),
    )
    assert result["state"] == "CLOSED"
    assert router.calls == []


@async_test
async def test_diagnostic_contains_full_dto_and_structured_field_differences() -> None:
    repository = ServiceRepository()
    east = DiagnosticProvider(ProviderCode.EASTMONEY, "10.01")
    sina = DiagnosticProvider(ProviderCode.SINA, "10.02")
    service = ProviderService(
        ProbeRouter(
            {ProviderCode.EASTMONEY: east, ProviderCode.SINA: sina}
        ),
        {ProviderCode.EASTMONEY: east, ProviderCode.SINA: sina},
        repository,
        ProbeRuntime(),
    )
    result = await service.quote_diagnostics(
        ("600000.SH",), reason="compare", audit_context=audit_context()
    )
    item = result["sources"][0]["items"][0]
    assert set(item) == {
        "symbol",
        "price",
        "open",
        "high",
        "low",
        "previous_close",
        "volume",
        "amount",
        "quote_time",
        "received_at",
        "source",
    }
    comparison = result["comparisons"][0]
    assert comparison["missing_sources"] == []
    assert comparison["status"] == "MATCH"
    assert comparison["differences"]["price"] == {
        "EASTMONEY": "10.01",
        "SINA": "10.02",
        "equal": False,
    }
    assert repository.diagnostic[0] == ("600000.SH",)


@async_test
async def test_diagnostic_reports_missing_source_without_fabricating_values() -> None:
    repository = ServiceRepository()
    east = DiagnosticProvider(ProviderCode.EASTMONEY, "10.01")
    service = ProviderService(
        ProbeRouter({ProviderCode.EASTMONEY: east}),
        {ProviderCode.EASTMONEY: east},
        repository,
        ProbeRuntime(),
    )
    result = await service.quote_diagnostics(
        ("600000.SH",), reason="compare", audit_context=audit_context()
    )
    comparison = result["comparisons"][0]
    assert comparison["status"] == "INCOMPLETE"
    assert comparison["missing_sources"] == ["SINA"]
    assert comparison["differences"]["price"]["SINA"] is None


@async_test
async def test_diagnostic_marks_material_price_difference_as_conflict() -> None:
    repository = ServiceRepository()
    east = DiagnosticProvider(ProviderCode.EASTMONEY, "10.00")
    sina = DiagnosticProvider(ProviderCode.SINA, "10.03")
    service = ProviderService(
        ProbeRouter(
            {ProviderCode.EASTMONEY: east, ProviderCode.SINA: sina}
        ),
        {ProviderCode.EASTMONEY: east, ProviderCode.SINA: sina},
        repository,
        ProbeRuntime(),
    )
    result = await service.quote_diagnostics(
        ("600000.SH",), reason="compare", audit_context=audit_context()
    )
    assert result["comparisons"][0]["status"] == "CONFLICT"
