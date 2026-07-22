from __future__ import annotations

import hashlib
import json

from long_invest.modules.backtests.contracts import (
    BacktestErrorCode,
    BacktestMode,
    BacktestUniverseEntry,
    BacktestUniverseSelection,
    BacktestUniverseSourcePort,
    FrozenBacktestUniverse,
)
from long_invest.platform.errors import AppError


class BacktestUniverseFreezer:
    def __init__(self, source: BacktestUniverseSourcePort) -> None:
        self._source = source

    async def freeze(
        self, selection: BacktestUniverseSelection
    ) -> FrozenBacktestUniverse:
        if selection.mode is BacktestMode.SINGLE:
            assert selection.symbol is not None
            entries = (await self._source.get_single(selection.symbol),)
        elif selection.mode is BacktestMode.WATCHLIST:
            assert selection.watchlist_id is not None
            entries = await self._source.list_watchlist(selection.watchlist_id)
        else:
            entries = await self._source.list_market()

        frozen = _deduplicate_and_sort(entries)
        if not frozen:
            raise AppError(
                code=BacktestErrorCode.BACKTEST_UNIVERSE_EMPTY.value,
                message="回测股票范围不能为空",
                status_code=422,
            )
        if selection.mode is BacktestMode.SINGLE and len(frozen) != 1:
            raise AppError(
                code="BACKTEST_SINGLE_SCOPE_INVALID",
                message="单股回测必须且只能冻结一只股票",
                status_code=409,
            )
        payload = [entry.model_dump(mode="json") for entry in frozen]
        content_hash = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        return FrozenBacktestUniverse(
            mode=selection.mode,
            entries=frozen,
            content_hash=content_hash,
            survivor_bias_disclosed=selection.mode is BacktestMode.MARKET,
        )


def _deduplicate_and_sort(
    entries: tuple[BacktestUniverseEntry, ...],
) -> tuple[BacktestUniverseEntry, ...]:
    by_security: dict[object, BacktestUniverseEntry] = {}
    symbols: dict[str, object] = {}
    for entry in entries:
        existing = by_security.get(entry.security_id)
        if existing is not None and existing != entry:
            raise AppError(
                code="BACKTEST_UNIVERSE_CONFLICT",
                message="回测股票范围包含互相冲突的股票快照",
                status_code=409,
            )
        existing_security = symbols.get(entry.symbol)
        if existing_security is not None and existing_security != entry.security_id:
            raise AppError(
                code="BACKTEST_UNIVERSE_CONFLICT",
                message="回测股票范围包含互相冲突的股票代码",
                status_code=409,
            )
        by_security[entry.security_id] = entry
        symbols[entry.symbol] = entry.security_id
    return tuple(sorted(by_security.values(), key=lambda entry: entry.symbol))
