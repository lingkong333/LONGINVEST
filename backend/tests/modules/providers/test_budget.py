import asyncio

from long_invest.modules.providers.budget import (
    BudgetLease,
    ProviderRequestBudget,
    enter_request_context,
    exit_request_context,
)
from long_invest.modules.providers.contracts import ProviderCapability, ProviderCode
from long_invest.modules.providers.models import ProviderBudgetPolicy
from long_invest.modules.providers.resilience import ProviderRouteSetting


def policy() -> ProviderBudgetPolicy:
    return ProviderBudgetPolicy(
        config_version=1,
        provider_code=ProviderCode.EASTMONEY.value,
        daily_limit=10,
        reset_timezone="Asia/Shanghai",
        max_concurrency=4,
        realtime_reserved=2,
        daily_reserved=2,
    )


def test_reserved_capacity_is_released_only_to_its_priority_class() -> None:
    item = policy()
    ceiling = ProviderRequestBudget._total_ceiling

    assert ceiling(item, ProviderCapability.HISTORICAL_DAILY_QFQ, {}) == 6
    assert ceiling(item, ProviderCapability.REALTIME_QUOTE_BATCH, {}) == 8
    assert ceiling(item, ProviderCapability.DAILY_BAR_UNADJUSTED, {}) == 8
    assert (
        ceiling(
            item,
            ProviderCapability.REALTIME_QUOTE_BATCH,
            {ProviderCapability.DAILY_BAR_UNADJUSTED.value: 2},
        )
        == 10
    )
    assert (
        ceiling(
            item,
            ProviderCapability.DAILY_BAR_UNADJUSTED,
            {ProviderCapability.REALTIME_QUOTE_BATCH.value: 2},
        )
        == 10
    )


def test_guard_claims_only_inside_provider_invocation_context() -> None:
    class RecordingBudget(ProviderRequestBudget):
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def claim(self, setting):
            self.calls.append(f"claim:{setting.capability.value}")
            return BudgetLease("lease")

        async def release(self, lease):
            self.calls.append(f"release:{lease.token}")

    async def run() -> list[str]:
        budget = RecordingBudget()
        async with budget.guard():
            pass
        setting = ProviderRouteSetting(
            ProviderCode.EASTMONEY,
            ProviderCapability.REALTIME_QUOTE_BATCH,
        )
        context_token = enter_request_context(setting)
        try:
            async with budget.guard():
                pass
        finally:
            exit_request_context(context_token)
        return budget.calls

    assert asyncio.run(run()) == [
        "claim:REALTIME_QUOTE_BATCH",
        "release:lease",
    ]
