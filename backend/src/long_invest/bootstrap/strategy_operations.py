from __future__ import annotations

from collections.abc import Callable

from long_invest.bootstrap.stage4_runtime import (
    build_backtest_application,
    build_target_application,
)
from long_invest.modules.backtests.contracts import (
    BacktestCreateRequest,
    BacktestDateRange,
)
from long_invest.modules.backtests.service import BacktestCommandContext
from long_invest.modules.monitoring.application import (
    get_monitor_subscription_application,
)
from long_invest.modules.strategies.application import get_strategy_application
from long_invest.modules.strategies.contracts import (
    StrategyStockTestSubmission,
    StrategySubscriptionCandidate,
    StrategySubscriptionScope,
    StrategyVersionTargetRequest,
    StrategyVersionTargetSubmission,
)
from long_invest.modules.targets.strategy_service import (
    ApplyStrategyTargetCommand,
    CalculateTargetCommand,
)


class StrategyStockTestAdapter:
    def __init__(
        self, application_factory: Callable = build_backtest_application
    ) -> None:
        self._application_factory = application_factory

    async def submit_strategy_test(
        self,
        *,
        task_id,
        draft,
        metadata,
        parameter_schema,
        request,
        idempotency_key,
        request_id,
        actor_user_id,
        reason,
    ) -> StrategyStockTestSubmission:
        state = await self._application_factory().create(
            task_id=task_id,
            request=BacktestCreateRequest(
                symbol=request.symbol,
                date_range=BacktestDateRange(
                    training_start_date=request.training_start_date,
                    training_end_date=request.training_end_date,
                    test_start_date=request.test_start_date,
                    test_end_date=request.test_end_date,
                ),
                draft_id=draft.id,
                draft_version=draft.draft_version,
                strategy_metadata=metadata,
                parameter_schema=parameter_schema,
                parameter_snapshot=request.parameter_snapshot,
                initial_capital=request.initial_capital,
            ),
            context=BacktestCommandContext(
                request_id=request_id,
                idempotency_key=idempotency_key,
                actor_user_id=actor_user_id,
                reason=reason,
            ),
        )
        return StrategyStockTestSubmission(
            task_id=state.task.id,
            status=state.task_status.value,
            replayed=False,
        )


class StrategySubscriptionScopeAdapter:
    async def resolve_strategy_subscriptions(
        self,
        *,
        strategy_id,
        scope: StrategySubscriptionScope,
        subscription_ids,
    ) -> tuple[StrategySubscriptionCandidate, ...]:
        monitoring = get_monitor_subscription_application()
        targets = build_target_application()
        strategies = get_strategy_application()
        owners = await monitoring.list(include_archived=False)
        requested = set(subscription_ids)
        candidates = []
        for owner in owners:
            if (
                scope is StrategySubscriptionScope.SELECTED
                and owner.id not in requested
            ):
                continue
            revisions = await monitoring.revisions(owner.id)
            revision = next(
                (item for item in revisions if item.id == owner.current_revision_id),
                None,
            )
            if revision is None or revision.strategy_version_id is None:
                continue
            version = await strategies.get_execution_snapshot(
                revision.strategy_version_id
            )
            if version is None or version.strategy_id != strategy_id:
                continue
            target = await targets.get(owner.id)
            candidates.append(
                StrategySubscriptionCandidate(
                    subscription_id=owner.id,
                    subscription_version=owner.version,
                    target_version=target.binding_version if target else 1,
                    parameter_snapshot=dict(revision.parameters),
                )
            )
        return tuple(sorted(candidates, key=lambda item: str(item.subscription_id)))


class StrategyVersionTargetAdapter:
    async def submit_strategy_version_target(
        self, request: StrategyVersionTargetRequest
    ) -> StrategyVersionTargetSubmission:
        result = await build_target_application().apply_strategy(
            ApplyStrategyTargetCommand(
                calculation=CalculateTargetCommand(
                    subscription_id=request.subscription_id,
                    target_date=request.target_date,
                    training_start_date=request.training_start_date,
                    training_end_date=request.training_end_date,
                    reason=request.reason,
                    expected_version=request.target_version,
                    idempotency_key=request.idempotency_key,
                    request_id=request.request_id,
                    actor_user_id=request.actor_user_id,
                    session_id=request.session_id,
                    trusted_ip=request.trusted_ip,
                ),
                strategy_version_id=request.strategy_version_id,
                parameter_snapshot=request.parameter_snapshot,
                expected_subscription_version=request.subscription_version,
            )
        )
        return StrategyVersionTargetSubmission(
            code=result.code,
            run_id=result.run_id,
            job_id=result.job_id,
            replayed=result.replayed,
        )


def build_strategy_operation_ports():
    return (
        StrategyStockTestAdapter(),
        StrategySubscriptionScopeAdapter(),
        StrategyVersionTargetAdapter(),
    )
