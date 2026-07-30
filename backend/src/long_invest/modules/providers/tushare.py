from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any

from long_invest.modules.providers.contracts import (
    DailyBar,
    DailyBarRequest,
    ProbeResult,
    ProviderAdapterCode,
    ProviderBatchResult,
    ProviderCapability,
    ProviderCode,
    ProviderSourceIdentity,
    SecurityMasterRecord,
)
from long_invest.modules.providers.retry import ProviderHttpError

TokenResolver = Callable[[], Awaitable[str | None]]
SdkLoader = Callable[..., Any]


class TushareProvider:
    code = ProviderCode.TUSHARE
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
        token_resolver: TokenResolver,
        sdk_loader: SdkLoader | None = None,
        request_guard: Callable[[], AbstractAsyncContextManager[None]] | None = None,
    ) -> None:
        self._token_resolver = token_resolver
        self._sdk_loader = sdk_loader or _load_tushare
        self._request_guard = request_guard

    def source_identity(self, capability: ProviderCapability) -> ProviderSourceIdentity:
        interface = {
            ProviderCapability.SECURITY_MASTER: "tushare.pro.stock_basic",
            ProviderCapability.DAILY_BAR_UNADJUSTED: "tushare.pro.daily",
            ProviderCapability.HISTORICAL_DAILY_UNADJUSTED: "tushare.pro.daily",
            ProviderCapability.HISTORICAL_DAILY_QFQ: "tushare.pro_bar",
        }.get(capability, "tushare.unsupported")
        return ProviderSourceIdentity(
            adapter=ProviderAdapterCode.TUSHARE_SDK,
            upstream=self.code,
            interface=interface,
            capability=capability,
            algorithm_version=(
                "tushare-qfq-v1"
                if capability is ProviderCapability.HISTORICAL_DAILY_QFQ
                else "raw-v1"
            ),
        )

    async def security_master(
        self, deadline: datetime
    ) -> tuple[SecurityMasterRecord, ...]:
        frames = []
        for status in ("L", "P", "D"):
            frames.append(
                await self._call_sdk(
                    "stock_basic",
                    deadline,
                    exchange="",
                    list_status=status,
                    fields=("ts_code,symbol,name,market,list_date,delist_date,is_hs"),
                )
            )
        return self.parse_security_master(frames, observed_at=datetime.now(UTC))

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
        method = (
            "pro_bar"
            if request.capability is ProviderCapability.HISTORICAL_DAILY_QFQ
            else "daily"
        )
        kwargs: dict[str, str] = {
            "ts_code": request.symbol,
            "start_date": request.start.strftime("%Y%m%d"),
            "end_date": request.end.strftime("%Y%m%d"),
        }
        if method == "pro_bar":
            kwargs["adj"] = "qfq"
        frame = await self._call_sdk(method, deadline, **kwargs)
        return self.parse_daily_bars(frame, request=request)

    async def corporate_actions(self, request, deadline):
        del request, deadline
        raise ProviderHttpError("PROVIDER_CAPABILITY_UNSUPPORTED")

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
                await self._call_sdk(
                    "stock_basic",
                    deadline,
                    exchange="SSE",
                    list_status="L",
                    fields="ts_code,symbol,name,market,list_date,delist_date,is_hs",
                )
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

    async def _call_sdk(self, method: str, deadline: datetime, **kwargs: str) -> Any:
        token = await self._token_resolver()
        if not token:
            raise ProviderHttpError("PROVIDER_CREDENTIAL_UNAVAILABLE")
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise TimeoutError
        try:
            async with asyncio.timeout(remaining):
                async with self._guard():
                    return await asyncio.to_thread(
                        self._sdk_loader, method=method, token=token, **kwargs
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

    @classmethod
    def parse_security_master(
        cls, frames: list[Any], *, observed_at: datetime
    ) -> tuple[SecurityMasterRecord, ...]:
        records: dict[str, SecurityMasterRecord] = {}
        try:
            for frame in frames:
                for row in frame.to_dict("records"):
                    symbol = str(row["ts_code"]).upper()
                    market = symbol[-2:]
                    if market not in {"SH", "SZ", "BJ"}:
                        continue
                    name = str(row["name"]).strip()
                    if not name:
                        raise ValueError
                    delisted_on = _date_or_none(row.get("delist_date"))
                    records[symbol] = SecurityMasterRecord(
                        symbol=symbol,
                        name=name,
                        market=market,
                        security_type="A_SHARE",
                        listed_on=_date_or_none(row.get("list_date")),
                        delisted_on=delisted_on,
                        listed=delisted_on is None,
                        is_st="ST" in name.upper(),
                        suspended=None,
                        source=ProviderCode.TUSHARE,
                        observed_at=observed_at,
                    )
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE") from error
        if not records:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
        return tuple(records[key] for key in sorted(records))

    @staticmethod
    def parse_daily_bars(
        frame: Any, *, request: DailyBarRequest
    ) -> ProviderBatchResult[DailyBar]:
        try:
            rows = frame.to_dict("records")
            bars = []
            seen: set[date] = set()
            for row in rows:
                trading_date = datetime.strptime(
                    str(row["trade_date"]), "%Y%m%d"
                ).date()
                if not request.start <= trading_date <= request.end:
                    continue
                if trading_date in seen:
                    raise ValueError
                seen.add(trading_date)
                bars.append(
                    DailyBar(
                        symbol=request.symbol,
                        trading_date=trading_date,
                        open=Decimal(str(row["open"])),
                        high=Decimal(str(row["high"])),
                        low=Decimal(str(row["low"])),
                        close=Decimal(str(row["close"])),
                        volume=int(Decimal(str(row["vol"])) * 100),
                        amount=Decimal(str(row["amount"])) * 1000,
                        source=ProviderCode.TUSHARE,
                        capability=request.capability,
                    )
                )
        except (
            AttributeError,
            InvalidOperation,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE") from error
        bars.sort(key=lambda item: item.trading_date)
        return ProviderBatchResult(tuple(bars))


def _date_or_none(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text or text.lower() == "nan":
        return None
    return datetime.strptime(text, "%Y%m%d").date()


def _load_tushare(*, method: str, token: str, **kwargs: str) -> Any:
    import tushare as ts

    pro = ts.pro_api(token)
    if method == "pro_bar":
        return ts.pro_bar(api=pro, **kwargs)
    return getattr(pro, method)(**kwargs)


class _NullRequestGuard:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: Any) -> None:
        del args
