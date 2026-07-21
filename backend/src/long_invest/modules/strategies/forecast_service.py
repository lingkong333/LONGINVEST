from __future__ import annotations

from typing import Protocol

from anyio import to_thread

from long_invest.modules.strategies.contracts import (
    StrategyForecastRequest,
    StrategyForecastRequestVerifier,
    StrategyForecastResult,
)
from long_invest.modules.strategies.forecast import (
    build_runner_payload,
    normalize_runner_result,
)


class StrategyRunnerPort(Protocol):
    def run(self, payload: dict[str, object]) -> object: ...


class SandboxedStrategyForecastService:
    def __init__(
        self,
        runner: StrategyRunnerPort,
        *,
        request_verifier: StrategyForecastRequestVerifier,
    ) -> None:
        self._runner = runner
        self._request_verifier = request_verifier

    async def forecast(
        self, request: StrategyForecastRequest
    ) -> StrategyForecastResult:
        if not await self._request_verifier.verify_forecast_request(request):
            from long_invest.modules.strategies.forecast import (
                INPUT_HASH_MISMATCH,
                StrategyForecastFailure,
            )

            raise StrategyForecastFailure(
                INPUT_HASH_MISMATCH,
                "strategy execution snapshot is not owned by its declared version",
            )
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
