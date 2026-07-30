from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any

from long_invest.modules.providers.contracts import (
    DailyBar,
    DailyBarRequest,
    DailyCollectionMode,
    DailyCollectionPlan,
    MarketDailyGroupRequest,
    ProbeResult,
    ProviderAdapterCode,
    ProviderBatchResult,
    ProviderCapability,
    ProviderCode,
    ProviderSourceIdentity,
    SecurityMasterRecord,
)
from long_invest.modules.providers.retry import ProviderHttpError


class BaoStockProvider:
    code = ProviderCode.BAOSTOCK
    capabilities = frozenset(
        {
            ProviderCapability.SECURITY_MASTER,
            ProviderCapability.DAILY_BAR_UNADJUSTED,
            ProviderCapability.HISTORICAL_DAILY_UNADJUSTED,
            ProviderCapability.HISTORICAL_DAILY_QFQ,
        }
    )

    def __init__(
        self,
        *,
        sdk_loader: Callable[..., list[dict[str, str]]] | None = None,
        request_guard: Callable[[], AbstractAsyncContextManager[None]] | None = None,
    ) -> None:
        self._sdk_loader = sdk_loader or _load_baostock
        self._request_guard = request_guard
        self._lock = asyncio.Lock()

    def source_identity(self, capability: ProviderCapability) -> ProviderSourceIdentity:
        return ProviderSourceIdentity(
            adapter=ProviderAdapterCode.BAOSTOCK_SDK,
            upstream=self.code,
            interface=(
                "baostock.query_all_stock"
                if capability is ProviderCapability.SECURITY_MASTER
                else "baostock.query_history_k_data_plus"
            ),
            capability=capability,
            algorithm_version=(
                "baostock-qfq-v1"
                if capability is ProviderCapability.HISTORICAL_DAILY_QFQ
                else "raw-v1"
            ),
        )

    async def security_master(
        self, deadline: datetime
    ) -> tuple[SecurityMasterRecord, ...]:
        rows = await self._call_sdk("security_master", deadline)
        return self.parse_security_master(rows, observed_at=datetime.now(UTC))

    async def realtime_quotes(self, symbols, deadline):
        del symbols, deadline
        raise ProviderHttpError("PROVIDER_CAPABILITY_UNSUPPORTED")

    async def daily_bars(
        self, request: DailyBarRequest, deadline: datetime
    ) -> ProviderBatchResult[DailyBar]:
        if (
            request.capability not in self.capabilities
            or request.capability is ProviderCapability.SECURITY_MASTER
        ):
            raise ProviderHttpError("PROVIDER_CAPABILITY_UNSUPPORTED")
        rows = await self._call_sdk(
            "daily_bars",
            deadline,
            symbol=_baostock_symbol(request.symbol),
            start=request.start.isoformat(),
            end=request.end.isoformat(),
            adjustflag=(
                "2"
                if request.capability is ProviderCapability.HISTORICAL_DAILY_QFQ
                else "3"
            ),
        )
        return self.parse_daily_bars(rows, request=request)

    async def corporate_actions(self, request, deadline):
        del request, deadline
        raise ProviderHttpError("PROVIDER_CAPABILITY_UNSUPPORTED")

    def daily_collection_plan(self, total_symbols: int) -> DailyCollectionPlan:
        return DailyCollectionPlan(
            self.code,
            DailyCollectionMode.SINGLE_SYMBOL,
            total_symbols,
            1,
            1,
        )

    async def market_daily_bars(
        self, request: MarketDailyGroupRequest, deadline: datetime
    ) -> ProviderBatchResult[DailyBar]:
        if len(request.symbols) != 1:
            raise ProviderHttpError("PROVIDER_GROUP_SIZE_INVALID")
        return await self.daily_bars(
            DailyBarRequest(
                request.symbols[0],
                request.trading_date,
                request.trading_date,
                ProviderCapability.DAILY_BAR_UNADJUSTED,
            ),
            deadline,
        )

    async def probe(
        self, capability: ProviderCapability, deadline: datetime
    ) -> ProbeResult:
        started = monotonic()
        if capability not in self.capabilities:
            return self._probe_result(
                capability, started, "PROVIDER_CAPABILITY_UNSUPPORTED"
            )
        try:
            if capability is ProviderCapability.SECURITY_MASTER:
                await self.security_master(deadline)
            else:
                today = datetime.now().date()
                await self.daily_bars(
                    DailyBarRequest(
                        "600000.SH", today - timedelta(days=10), today, capability
                    ),
                    deadline,
                )
            return self._probe_result(capability, started, None)
        except Exception as error:
            return self._probe_result(
                capability, started, getattr(error, "code", "PROVIDER_FAILED")
            )

    async def _call_sdk(self, operation: str, deadline: datetime, **kwargs: str) -> Any:
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise TimeoutError
        try:
            async with asyncio.timeout(remaining):
                async with self._guard():
                    async with self._lock:
                        return await asyncio.to_thread(
                            self._sdk_loader, operation=operation, **kwargs
                        )
        except TimeoutError:
            raise
        except ProviderHttpError:
            raise
        except (ImportError, ModuleNotFoundError) as error:
            raise ProviderHttpError("PROVIDER_DEPENDENCY_UNAVAILABLE") from error
        except Exception as error:
            raise ProviderHttpError(
                "PROVIDER_UPSTREAM_FAILED", retryable=True
            ) from error

    def _guard(self) -> AbstractAsyncContextManager[None]:
        return self._request_guard() if self._request_guard else _NullRequestGuard()

    def _probe_result(
        self, capability: ProviderCapability, started: float, error_code: str | None
    ) -> ProbeResult:
        return ProbeResult(
            self.code,
            capability,
            error_code is None,
            datetime.now(UTC),
            int((monotonic() - started) * 1000),
            error_code,
        )

    @staticmethod
    def parse_security_master(
        rows: list[dict[str, str]], *, observed_at: datetime
    ) -> tuple[SecurityMasterRecord, ...]:
        records = []
        seen: set[str] = set()
        try:
            for row in rows:
                raw_code = row["code"].lower()
                code = raw_code[3:]
                is_a_share = (
                    raw_code.startswith("sh.")
                    and code.startswith("6")
                    or raw_code.startswith("sz.")
                    and code.startswith(("0", "3"))
                )
                if not is_a_share:
                    continue
                symbol = f"{code}.{raw_code[:2].upper()}"
                name = row["code_name"].strip()
                if not name or symbol in seen:
                    raise ValueError
                seen.add(symbol)
                records.append(
                    SecurityMasterRecord(
                        symbol=symbol,
                        name=name,
                        market=symbol[-2:],
                        security_type="A_SHARE",
                        listed_on=None,
                        delisted_on=None,
                        listed=True,
                        is_st="ST" in name.upper(),
                        suspended=row.get("tradeStatus") == "0",
                        source=ProviderCode.BAOSTOCK,
                        observed_at=observed_at,
                    )
                )
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE") from error
        if not records:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
        return tuple(sorted(records, key=lambda item: item.symbol))

    @staticmethod
    def parse_daily_bars(
        rows: list[dict[str, str]], *, request: DailyBarRequest
    ) -> ProviderBatchResult[DailyBar]:
        bars = []
        seen = set()
        try:
            for row in rows:
                if row.get("tradestatus") == "0" or not row.get("open"):
                    continue
                trading_date = datetime.strptime(row["date"], "%Y-%m-%d").date()
                if not request.start <= trading_date <= request.end:
                    continue
                if trading_date in seen:
                    raise ValueError
                seen.add(trading_date)
                bars.append(
                    DailyBar(
                        symbol=request.symbol,
                        trading_date=trading_date,
                        open=Decimal(row["open"]),
                        high=Decimal(row["high"]),
                        low=Decimal(row["low"]),
                        close=Decimal(row["close"]),
                        volume=int(Decimal(row["volume"] or "0")),
                        amount=Decimal(row["amount"] or "0"),
                        source=ProviderCode.BAOSTOCK,
                        capability=request.capability,
                    )
                )
        except (InvalidOperation, KeyError, TypeError, ValueError) as error:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE") from error
        bars.sort(key=lambda item: item.trading_date)
        return ProviderBatchResult(tuple(bars))


def _baostock_symbol(symbol: str) -> str:
    return f"{symbol[-2:].lower()}.{symbol[:6]}"


def _load_baostock(*, operation: str, **kwargs: str) -> list[dict[str, str]]:
    import baostock as bs

    login = bs.login()
    if login.error_code != "0":
        raise ProviderHttpError("PROVIDER_AUTH_FAILED")
    try:
        if operation == "security_master":
            result = bs.query_all_stock(day=datetime.now().strftime("%Y-%m-%d"))
        else:
            result = bs.query_history_k_data_plus(
                kwargs["symbol"],
                "date,code,open,high,low,close,preclose,volume,amount,tradestatus",
                start_date=kwargs["start"],
                end_date=kwargs["end"],
                frequency="d",
                adjustflag=kwargs["adjustflag"],
            )
        if result.error_code != "0":
            raise ProviderHttpError("PROVIDER_UPSTREAM_FAILED", retryable=True)
        rows = []
        while result.next():
            rows.append(dict(zip(result.fields, result.get_row_data(), strict=True)))
        return rows
    finally:
        bs.logout()


class _NullRequestGuard:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: Any) -> None:
        del args
