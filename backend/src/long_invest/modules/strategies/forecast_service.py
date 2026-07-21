from __future__ import annotations

from typing import Protocol

from anyio import to_thread

from long_invest.modules.strategies.contracts import (
    StrategyForecastRequest,
    StrategyForecastResult,
)
from long_invest.modules.strategies.forecast import (
    build_runner_payload,
    normalize_runner_result,
)


class StrategyRunnerPort(Protocol):
    def run(self, payload: dict[str, object]) -> object: ...


class SandboxedStrategyForecastService:
    def __init__(self, runner: StrategyRunnerPort) -> None:
        self._runner = runner

    async def forecast(
        self, request: StrategyForecastRequest
    ) -> StrategyForecastResult:
        execution_id = request.strategy_version_id or request.draft_id
        payload = build_runner_payload(
            request=request,
            context={
                "symbol": request.training_data.symbol,
                "exchange": request.training_data.symbol.rsplit(".", maxsplit=1)[1],
                "name": request.security_name,
                "as_of_date": request.training_data.end_date,
                "strategy_version_id": str(execution_id),
                "data_version": request.training_data.data_version,
                "calculation_reason": "TARGET_FORECAST",
            },
        )
        raw_result = await to_thread.run_sync(self._runner.run, payload)
        return normalize_runner_result(raw_result)
