from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from long_invest.bootstrap import strategy_operations
from long_invest.bootstrap.strategy_operations import (
    StrategyStockTestAdapter,
    StrategySubscriptionScopeAdapter,
    StrategyVersionTargetAdapter,
)
from long_invest.modules.strategies.contracts import (
    StrategyDraftView,
    StrategyStockTestRequest,
    StrategySubscriptionScope,
    StrategyVersionOperation,
    StrategyVersionTargetRequest,
)


@pytest.mark.anyio
async def test_stock_test_adapter_preserves_frozen_draft_and_dates() -> None:
    task_id = uuid4()
    application = SimpleNamespace(
        create=AsyncMock(
            return_value=SimpleNamespace(
                task=SimpleNamespace(id=task_id),
                task_status=SimpleNamespace(value="PENDING"),
                execution_generation=1,
            )
        )
    )
    draft = StrategyDraftView(
        id=uuid4(),
        strategy_id=uuid4(),
        draft_version=3,
        source_code="def strategy(): pass",
    )
    request = StrategyStockTestRequest(
        strategy_id=draft.strategy_id,
        symbol="600000.SH",
        training_start_date=date(2010, 1, 1),
        training_end_date=date(2020, 12, 31),
        test_start_date=date(2021, 1, 1),
        test_end_date=date(2022, 12, 31),
        parameter_snapshot={"window": 20},
        initial_capital=Decimal("100000"),
    )

    result = await StrategyStockTestAdapter(lambda: application).submit_strategy_test(
        task_id=task_id,
        draft=draft,
        metadata={"name": "test"},
        parameter_schema={"type": "object"},
        request=request,
        idempotency_key="test-1",
        request_id="req-1",
        actor_user_id="user-1",
        reason="single stock test",
    )

    submitted = application.create.await_args.kwargs["request"]
    assert submitted.draft_id == draft.id
    assert submitted.draft_version == 3
    assert submitted.date_range.test_start_date == date(2021, 1, 1)
    assert result.task_id == task_id
    assert result.status == "PENDING"


@pytest.mark.anyio
async def test_scope_adapter_filters_by_strategy_and_uses_revision_parameters(
    monkeypatch,
) -> None:
    strategy_id = uuid4()
    matching_version_id = uuid4()
    other_version_id = uuid4()
    matching = SimpleNamespace(id=uuid4(), current_revision_id=uuid4(), version=4)
    unrelated = SimpleNamespace(id=uuid4(), current_revision_id=uuid4(), version=2)
    monitoring = SimpleNamespace(
        list=AsyncMock(return_value=[matching, unrelated]),
        revisions=AsyncMock(
            side_effect=[
                [
                    SimpleNamespace(
                        id=matching.current_revision_id,
                        strategy_version_id=matching_version_id,
                        parameters={"window": 60},
                    )
                ],
                [
                    SimpleNamespace(
                        id=unrelated.current_revision_id,
                        strategy_version_id=other_version_id,
                        parameters={"window": 10},
                    )
                ],
            ]
        ),
    )
    targets = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(binding_version=7))
    )
    strategies = SimpleNamespace(
        get_execution_snapshot=AsyncMock(
            side_effect=[
                SimpleNamespace(strategy_id=strategy_id),
                SimpleNamespace(strategy_id=uuid4()),
            ]
        )
    )
    monkeypatch.setattr(
        strategy_operations, "get_monitor_subscription_application", lambda: monitoring
    )
    monkeypatch.setattr(
        strategy_operations, "build_target_application", lambda: targets
    )
    monkeypatch.setattr(
        strategy_operations, "get_strategy_application", lambda: strategies
    )

    result = await StrategySubscriptionScopeAdapter().resolve_strategy_subscriptions(
        strategy_id=strategy_id,
        scope=StrategySubscriptionScope.ALL_RELATED,
        subscription_ids=(),
    )

    assert len(result) == 1
    assert result[0].subscription_id == matching.id
    assert result[0].subscription_version == 4
    assert result[0].target_version == 7
    assert result[0].parameter_snapshot == {"window": 60}


@pytest.mark.anyio
async def test_target_adapter_submits_one_isolated_target_calculation(
    monkeypatch,
) -> None:
    target_application = SimpleNamespace(
        apply_strategy=AsyncMock(
            return_value=SimpleNamespace(
                code="TARGET_CALCULATION_REQUESTED",
                run_id=uuid4(),
                job_id=uuid4(),
                replayed=False,
            )
        )
    )
    monkeypatch.setattr(
        strategy_operations,
        "build_target_application",
        lambda: target_application,
    )
    request = StrategyVersionTargetRequest(
        operation=StrategyVersionOperation.APPLY,
        strategy_id=uuid4(),
        strategy_version_id=uuid4(),
        subscription_id=uuid4(),
        subscription_version=4,
        target_version=7,
        parameter_snapshot={"window": 60},
        target_date=date(2026, 7, 22),
        training_start_date=date(2016, 1, 1),
        training_end_date=date(2025, 12, 31),
        reason="apply version",
        idempotency_key="apply-1",
        request_id="req-1",
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
    )

    result = await StrategyVersionTargetAdapter().submit_strategy_version_target(
        request
    )

    command = target_application.apply_strategy.await_args.args[0]
    assert command.strategy_version_id == request.strategy_version_id
    assert command.expected_subscription_version == 4
    assert command.calculation.expected_version == 7
    assert command.calculation.training_end_date == date(2025, 12, 31)
    assert result.code == "TARGET_CALCULATION_REQUESTED"
