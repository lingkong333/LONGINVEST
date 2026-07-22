import asyncio
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.modules.strategies.application import StrategyApplication
from long_invest.modules.strategies.contracts import (
    StrategyOperationItemStatus,
    StrategyStockTestRequest,
    StrategyStockTestSubmission,
    StrategySubscriptionCandidate,
    StrategySubscriptionScope,
    StrategyVersionOperation,
    StrategyVersionTargetSubmission,
)
from long_invest.modules.strategies.service import (
    FrozenVersionOperation,
    StrategyCommandContext,
    StrategyService,
)
from long_invest.platform.errors import AppError

SOURCE = """\
STRATEGY_API_VERSION = "1.0"
STRATEGY_META = {
    "name": "test",
    "data_requirements": {"adjustment": "qfq", "min_bars": 10, "max_bars": 100},
    "parameter_schema": {"type": "object", "additionalProperties": False},
}
def calculate_targets(history, params, context):
    return {"low_strong": 1, "low_watch": 2, "high_watch": 3, "high_strong": 4}
"""


class Database:
    @asynccontextmanager
    async def transaction(self):
        yield SimpleNamespace()

    @asynccontextmanager
    async def session(self):
        yield SimpleNamespace()


class OperationsService:
    def __init__(self, draft):
        self.draft = draft
        self.requests = []
        self.replayed = False
        self.frozen_ids = None

    async def get_draft(self, _strategy_id):
        return self.draft

    async def request_version_operation(self, strategy_id, **kwargs):
        self.requests.append((strategy_id, kwargs))
        ids = self.frozen_ids or tuple(sorted(kwargs["subscription_ids"], key=str))
        return FrozenVersionOperation(
            strategy_version_id=kwargs["strategy_version_id"],
            subscription_ids=ids,
            replayed=self.replayed,
        )


class StockTests:
    def __init__(self):
        self.calls = []

    async def submit_strategy_test(self, **kwargs):
        self.calls.append(kwargs)
        return StrategyStockTestSubmission(task_id=kwargs["task_id"], status="PENDING")


class SubscriptionScope:
    def __init__(self, candidates):
        self.candidates = {item.subscription_id: item for item in candidates}
        self.calls = []

    async def resolve_strategy_subscriptions(self, **kwargs):
        self.calls.append(kwargs)
        ids = kwargs["subscription_ids"]
        if kwargs["scope"] is StrategySubscriptionScope.ALL_RELATED:
            return tuple(self.candidates.values())
        return tuple(
            self.candidates[value] for value in ids if value in self.candidates
        )


class VersionTargets:
    def __init__(self, *, rejected=None, failed=None):
        self.rejected = rejected
        self.failed = failed
        self.calls = []

    async def submit_strategy_version_target(self, request):
        self.calls.append(request)
        if request.subscription_id == self.rejected:
            raise AppError(
                code="TARGET_VERSION_CONFLICT", message="conflict", status_code=409
            )
        if request.subscription_id == self.failed:
            raise TimeoutError
        return StrategyVersionTargetSubmission(
            code="TARGET_CALCULATION_ACCEPTED",
            run_id=uuid4(),
            job_id=uuid4(),
            replayed=False,
        )


def operation_application(service, *, stock_tests=None, scope=None, targets=None):
    return StrategyApplication(
        Database(),
        git_store=SimpleNamespace(),
        evidence_verifier=SimpleNamespace(),
        repository_factory=lambda _session: SimpleNamespace(),
        audit_factory=lambda _session: SimpleNamespace(),
        event_factory=lambda _session: SimpleNamespace(),
        service_factory=lambda *_args, **_kwargs: service,
        stock_tests=stock_tests,
        subscription_scope=scope,
        version_targets=targets,
    )


def operation_kwargs(subscription_ids):
    return {
        "scope": StrategySubscriptionScope.SELECTED,
        "subscription_ids": subscription_ids,
        "target_date": date(2026, 7, 22),
        "training_start_date": date(2010, 1, 1),
        "training_end_date": date(2020, 12, 31),
        "reason": "apply release",
        "idempotency_key": "operation-1",
        "request_id": "request-1",
        "actor_user_id": "user-1",
        "session_id": "session-1",
        "trusted_ip": "127.0.0.1",
    }


def context(key="operation-1"):
    return StrategyCommandContext(
        request_id="request-1",
        idempotency_key=key,
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
        reason="apply release",
    )


def operation_dates():
    return {
        "target_date": date(2026, 7, 22),
        "training_start_date": date(2016, 1, 1),
        "training_end_date": date(2025, 12, 31),
    }


def test_stock_test_freezes_current_draft_and_reuses_deterministic_task_id():
    strategy_id = uuid4()
    draft = SimpleNamespace(
        id=uuid4(), strategy_id=strategy_id, draft_version=3, source_code=SOURCE
    )
    service = OperationsService(draft)
    stock_tests = StockTests()
    subject = operation_application(service, stock_tests=stock_tests)
    request = StrategyStockTestRequest(
        strategy_id=strategy_id,
        symbol="600000.SH",
        training_start_date=date(2010, 1, 1),
        training_end_date=date(2020, 12, 31),
        test_start_date=date(2021, 1, 1),
        test_end_date=date(2022, 12, 31),
        parameter_snapshot={},
        initial_capital=Decimal("100000"),
    )

    first = asyncio.run(
        subject.test_stock(
            request,
            idempotency_key="test-1",
            request_id="request-1",
            actor_user_id="user-1",
            reason="test draft",
        )
    )
    replay = asyncio.run(
        subject.test_stock(
            request,
            idempotency_key="test-1",
            request_id="request-1",
            actor_user_id="user-1",
            reason="test draft",
        )
    )

    assert first.task_id == replay.task_id
    assert stock_tests.calls[0]["draft"].draft_version == 3
    assert stock_tests.calls[0]["metadata"]["name"] == "test"


def test_apply_version_isolates_missing_rejected_and_temporary_failures():
    strategy_id = uuid4()
    version_id = uuid4()
    accepted, rejected, failed, missing = (uuid4() for _ in range(4))
    candidates = tuple(
        StrategySubscriptionCandidate(
            subscription_id=value,
            subscription_version=2,
            target_version=4,
            parameter_snapshot={},
        )
        for value in (accepted, rejected, failed)
    )
    service = OperationsService(None)
    scope = SubscriptionScope(candidates)
    targets = VersionTargets(rejected=rejected, failed=failed)
    subject = operation_application(service, scope=scope, targets=targets)

    result = asyncio.run(
        subject.apply_version(
            strategy_id,
            version_id,
            **operation_kwargs((accepted, rejected, failed, missing)),
        )
    )
    status_by_id = {item.subscription_id: item.status for item in result.items}

    assert status_by_id == {
        accepted: StrategyOperationItemStatus.ACCEPTED,
        rejected: StrategyOperationItemStatus.REJECTED,
        failed: StrategyOperationItemStatus.FAILED,
        missing: StrategyOperationItemStatus.REJECTED,
    }
    assert len(targets.calls) == 3


def test_replayed_all_related_operation_keeps_original_subscription_scope():
    strategy_id = uuid4()
    version_id = uuid4()
    original, newly_related = uuid4(), uuid4()
    candidates = tuple(
        StrategySubscriptionCandidate(
            subscription_id=value,
            subscription_version=1,
            target_version=1,
            parameter_snapshot={},
        )
        for value in (original, newly_related)
    )
    service = OperationsService(None)
    service.replayed = True
    service.frozen_ids = (original,)
    scope = SubscriptionScope(candidates)
    targets = VersionTargets()
    subject = operation_application(service, scope=scope, targets=targets)
    kwargs = operation_kwargs(())
    kwargs["scope"] = StrategySubscriptionScope.ALL_RELATED

    result = asyncio.run(subject.rollback_version(strategy_id, version_id, **kwargs))

    assert result.replayed is True
    assert [item.subscription_id for item in result.items] == [original]
    assert [call.subscription_id for call in targets.calls] == [original]
    assert len(scope.calls) == 2
    assert scope.calls[-1]["scope"] is StrategySubscriptionScope.SELECTED


class Repository:
    def __init__(self, strategy, version):
        self.strategy = strategy
        self.version = version

    async def get_strategy(self, strategy_id, *, for_update=False):
        return self.strategy if strategy_id == self.strategy.id else None

    async def get_version(self, strategy_id, version_id, *, for_update=False):
        if strategy_id == self.strategy.id and version_id == self.version.id:
            return self.version
        return None


class Audit:
    def __init__(self):
        self.items = []

    async def append(self, item):
        self.items.append(item)

    async def find_by_idempotency(self, key):
        return next((item for item in self.items if item.idempotency_key == key), None)


class Events:
    def __init__(self):
        self.items = []

    async def emit(self, item):
        self.items.append(item)


def test_version_operation_request_is_audited_emitted_and_idempotent():
    strategy = SimpleNamespace(id=uuid4(), status="PUBLISHED")
    version = SimpleNamespace(id=uuid4(), status="PUBLISHED")
    subscription_ids = (uuid4(), uuid4())
    audit = Audit()
    events = Events()
    subject = StrategyService(Repository(strategy, version), audit=audit, events=events)

    first = asyncio.run(
        subject.request_version_operation(
            strategy.id,
            strategy_version_id=version.id,
            operation=StrategyVersionOperation.APPLY,
            scope=StrategySubscriptionScope.SELECTED,
            subscription_ids=subscription_ids,
            **operation_dates(),
            context=context(),
        )
    )
    replay = asyncio.run(
        subject.request_version_operation(
            strategy.id,
            strategy_version_id=version.id,
            operation=StrategyVersionOperation.APPLY,
            scope=StrategySubscriptionScope.SELECTED,
            subscription_ids=tuple(reversed(subscription_ids)),
            **operation_dates(),
            context=context(),
        )
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert len(audit.items) == 1
    assert events.items[0].topic == "strategy.version_apply_requested"

    with pytest.raises(AppError) as raised:
        asyncio.run(
            subject.request_version_operation(
                strategy.id,
                strategy_version_id=version.id,
                operation=StrategyVersionOperation.APPLY,
                scope=StrategySubscriptionScope.SELECTED,
                subscription_ids=(uuid4(),),
                **operation_dates(),
                context=context(),
            )
        )
    assert raised.value.code == "STRATEGY_IDEMPOTENCY_CONFLICT"

    changed_dates = operation_dates()
    changed_dates["target_date"] = date(2026, 7, 23)
    with pytest.raises(AppError) as changed_date:
        asyncio.run(
            subject.request_version_operation(
                strategy.id,
                strategy_version_id=version.id,
                operation=StrategyVersionOperation.APPLY,
                scope=StrategySubscriptionScope.SELECTED,
                subscription_ids=subscription_ids,
                **changed_dates,
                context=context(),
            )
        )
    assert changed_date.value.code == "STRATEGY_IDEMPOTENCY_CONFLICT"


def test_version_operation_rejects_unpublished_version_before_event():
    strategy = SimpleNamespace(id=uuid4(), status="DRAFT")
    version = SimpleNamespace(id=uuid4(), status="PUBLISHING")
    audit = Audit()
    events = Events()
    subject = StrategyService(Repository(strategy, version), audit=audit, events=events)

    with pytest.raises(AppError) as raised:
        asyncio.run(
            subject.request_version_operation(
                strategy.id,
                strategy_version_id=version.id,
                operation=StrategyVersionOperation.APPLY,
                scope=StrategySubscriptionScope.SELECTED,
                subscription_ids=(uuid4(),),
                **operation_dates(),
                context=context(),
            )
        )

    assert raised.value.code == "STRATEGY_VERSION_NOT_PUBLISHED"
    assert audit.items == []
    assert events.items == []


def test_all_related_replay_returns_original_scope_when_new_subscription_exists():
    strategy = SimpleNamespace(id=uuid4(), status="PUBLISHED")
    version = SimpleNamespace(id=uuid4(), status="PUBLISHED")
    original, newly_related = uuid4(), uuid4()
    audit = Audit()
    subject = StrategyService(
        Repository(strategy, version), audit=audit, events=Events()
    )

    asyncio.run(
        subject.request_version_operation(
            strategy.id,
            strategy_version_id=version.id,
            operation=StrategyVersionOperation.APPLY,
            scope=StrategySubscriptionScope.ALL_RELATED,
            subscription_ids=(original,),
            **operation_dates(),
            context=context(),
        )
    )
    replay = asyncio.run(
        subject.request_version_operation(
            strategy.id,
            strategy_version_id=version.id,
            operation=StrategyVersionOperation.APPLY,
            scope=StrategySubscriptionScope.ALL_RELATED,
            subscription_ids=(original, newly_related),
            **operation_dates(),
            context=context(),
        )
    )

    assert replay.replayed is True
    assert replay.subscription_ids == (original,)
