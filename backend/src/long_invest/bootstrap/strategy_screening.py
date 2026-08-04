from __future__ import annotations

from long_invest.bootstrap.stage4_runtime import LazyStrategyForecastService
from long_invest.bootstrap.strategy_data import QfqStrategyDataPort
from long_invest.modules.qfq.application import get_qfq_application
from long_invest.modules.securities.application import get_security_application
from long_invest.modules.strategies.application import get_strategy_application
from long_invest.modules.strategies.screening import StrategyScreeningApplication
from long_invest.platform.database.engine import get_database


class ScreeningScopeAdapter:
    def __init__(self) -> None:
        self._securities = get_security_application()

    async def list_market(self) -> tuple[object, ...]:
        result = []
        page = 1
        while True:
            rows, total = await self._securities.list(page=page, page_size=200)
            result.extend(
                item
                for item in rows
                if str(item.market) in {"SH", "SZ", "BJ"}
                and str(item.security_type) == "A_SHARE"
                and str(item.listing_status) in {"LISTED", "SUSPENDED"}
            )
            if page * 200 >= total:
                return tuple(result)
            page += 1

    async def freeze_in_transaction(self, session):
        return await self._securities.freeze_universe_in_transaction(session)


class ScreeningDatasetAdapter:
    def __init__(self) -> None:
        self._qfq = get_qfq_application()
        self._data = QfqStrategyDataPort(self._qfq)

    async def current_dataset_snapshots(self, security_ids):
        return await self._qfq.current_dataset_snapshots(security_ids)

    async def get_training_data_from_dataset(self, **kwargs):
        return await self._data.get_training_data_from_dataset(**kwargs)


def build_strategy_screening_application() -> StrategyScreeningApplication:
    return StrategyScreeningApplication(
        get_database(),
        strategies=get_strategy_application(),
        scope=ScreeningScopeAdapter(),
        datasets=ScreeningDatasetAdapter(),
        forecasts=LazyStrategyForecastService(),
    )


async def strategy_screening_batch(context):
    return await build_strategy_screening_application().run(context)
