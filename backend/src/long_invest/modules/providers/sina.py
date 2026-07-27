from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any

from long_invest.modules.providers.contracts import (
    DailyBar,
    DailyBarRequest,
    ProbeResult,
    ProviderBatchResult,
    ProviderCapability,
    ProviderCode,
    ProviderItemFailure,
    RealtimeQuote,
    SecurityMasterRecord,
)
from long_invest.modules.providers.http_client import (
    ProviderHttpClient,
    ProviderHttpRequest,
)
from long_invest.modules.providers.retry import ProviderHttpError

LINE = re.compile(r'^var hq_str_(sh|sz|bj)(\d{6})="(.*)";$')
CHINA_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


class SinaRealtimeProvider:
    code = ProviderCode.SINA
    capabilities = frozenset(
        {
            ProviderCapability.SECURITY_MASTER,
            ProviderCapability.REALTIME_QUOTE_BATCH,
            ProviderCapability.HISTORICAL_DAILY_UNADJUSTED,
            ProviderCapability.HISTORICAL_DAILY_QFQ,
        }
    )
    REALTIME_URL = "https://hq.sinajs.cn/list="
    REFERER = "https://finance.sina.com.cn/"
    MASTER_COUNT_URL = (
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "Market_Center.getHQNodeStockCount"
    )
    MASTER_PAGE_URL = (
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "Market_Center.getHQNodeData"
    )
    MASTER_PAGE_SIZE = 100
    MASTER_MAX_PAGES = 100

    def __init__(
        self,
        client: ProviderHttpClient | None,
        *,
        history_loader: Callable[..., Any] | None = None,
    ) -> None:
        self._client = client
        self._history_loader = history_loader

    async def realtime_quotes(
        self, symbols: tuple[str, ...], deadline: datetime
    ) -> ProviderBatchResult[RealtimeQuote]:
        codes = ",".join(symbol[-2:].lower() + symbol[:6] for symbol in symbols)
        if self._client is None:
            raise RuntimeError("provider client is not configured")
        text = await self._client.request_text(
            ProviderHttpRequest(
                self.REALTIME_URL + codes,
                headers={"Referer": self.REFERER},
            ),
            deadline=deadline,
            encoding="gb18030",
        )
        try:
            return self.parse_quotes(text, symbols, received_at=datetime.now(UTC))
        except ProviderHttpError as error:
            if error.code == "PROVIDER_SCHEMA_INCOMPATIBLE":
                error.response_sample = {"body_excerpt": text[:2048]}
            raise

    async def security_master(self, deadline: datetime):
        if self._client is None:
            raise RuntimeError("provider client is not configured")
        total = await self._security_master_count(deadline)
        page_count = (total + self.MASTER_PAGE_SIZE - 1) // self.MASTER_PAGE_SIZE
        if page_count <= 0 or page_count > self.MASTER_MAX_PAGES:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")

        semaphore = asyncio.Semaphore(2)

        async def fetch(page: int) -> tuple[SecurityMasterRecord, ...]:
            async with semaphore:
                text = await self._client.request_text(
                    ProviderHttpRequest(
                        self.MASTER_PAGE_URL,
                        {
                            "page": str(page),
                            "num": str(self.MASTER_PAGE_SIZE),
                            "sort": "symbol",
                            "asc": "1",
                            "node": "hs_a",
                            "symbol": "",
                        },
                        headers=self._master_headers(),
                    ),
                    deadline=deadline,
                )
                return self.parse_security_master_page(
                    text, observed_at=datetime.now(UTC)
                )

        pages = await asyncio.gather(
            *(fetch(page) for page in range(1, page_count + 1))
        )
        records = tuple(record for page in pages for record in page)
        symbols = {record.symbol for record in records}
        if len(records) != total or len(symbols) != total:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
        return records

    async def _security_master_count(self, deadline: datetime) -> int:
        if self._client is None:
            raise RuntimeError("provider client is not configured")
        text = await self._client.request_text(
            ProviderHttpRequest(
                self.MASTER_COUNT_URL,
                {"node": "hs_a"},
                headers=self._master_headers(),
            ),
            deadline=deadline,
        )
        try:
            total = int(json.loads(text))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE") from error
        if total <= 0 or total > self.MASTER_PAGE_SIZE * self.MASTER_MAX_PAGES:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
        return total

    def _master_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": self.REFERER,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36"
            ),
        }

    async def daily_bars(
        self, request: DailyBarRequest, deadline: datetime
    ) -> ProviderBatchResult[DailyBar]:
        if request.capability not in {
            ProviderCapability.HISTORICAL_DAILY_UNADJUSTED,
            ProviderCapability.HISTORICAL_DAILY_QFQ,
        }:
            raise ProviderHttpError("PROVIDER_CAPABILITY_UNSUPPORTED")
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise TimeoutError
        loader = self._history_loader or _load_sina_history
        adjust = (
            "qfq"
            if request.capability is ProviderCapability.HISTORICAL_DAILY_QFQ
            else ""
        )
        try:
            async with asyncio.timeout(remaining):
                frame = await asyncio.to_thread(
                    loader,
                    symbol=_sina_symbol(request.symbol),
                    start_date=request.start.strftime("%Y%m%d"),
                    end_date=request.end.strftime("%Y%m%d"),
                    adjust=adjust,
                )
            return self.parse_daily_bars(frame, request=request)
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

    async def probe(
        self, capability: ProviderCapability, deadline: datetime
    ) -> ProbeResult:
        started = monotonic()
        if capability not in self.capabilities:
            return ProbeResult(
                self.code,
                capability,
                False,
                datetime.now(UTC),
                0,
                "PROVIDER_CAPABILITY_UNSUPPORTED",
            )
        try:
            if capability is ProviderCapability.SECURITY_MASTER:
                await self._security_master_count(deadline)
            elif capability in {
                ProviderCapability.HISTORICAL_DAILY_UNADJUSTED,
                ProviderCapability.HISTORICAL_DAILY_QFQ,
            }:
                today = datetime.now(CHINA_TIMEZONE).date()
                await self.daily_bars(
                    DailyBarRequest(
                        "600000.SH", today - timedelta(days=10), today, capability
                    ),
                    deadline,
                )
            else:
                await self.realtime_quotes(("600000.SH",), deadline)
            healthy, error_code = True, None
        except Exception as error:
            healthy, error_code = False, getattr(error, "code", "PROVIDER_FAILED")
        return ProbeResult(
            self.code,
            capability,
            healthy,
            datetime.now(UTC),
            int((monotonic() - started) * 1000),
            error_code,
        )

    def parse_quotes(
        self, text: str, symbols: tuple[str, ...], *, received_at: datetime
    ) -> ProviderBatchResult[RealtimeQuote]:
        if len(text.encode("utf-8")) > 256_000 or any(
            marker in text.lower()
            for marker in ("<html", "captcha", "validatecode", "error")
        ):
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
        parsed: dict[str, RealtimeQuote] = {}
        row_failures: dict[str, ProviderItemFailure] = {}
        for raw_line in filter(None, (line.strip() for line in text.splitlines())):
            match = LINE.fullmatch(raw_line)
            if not match:
                raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
            market, code, content = match.groups()
            symbol = f"{code}.{market.upper()}"
            if not content:
                continue
            fields = content.split(",")
            try:
                if len(fields) < 32:
                    raise ValueError("missing quote fields")
                quote_time = datetime.fromisoformat(
                    f"{fields[30]}T{fields[31]}"
                ).replace(tzinfo=CHINA_TIMEZONE)
                values = [Decimal(fields[index]) for index in (1, 2, 3, 4, 5, 8, 9)]
                parsed[symbol] = RealtimeQuote(
                    symbol,
                    values[2],
                    values[0],
                    values[3],
                    values[4],
                    values[1],
                    int(values[5]),
                    values[6],
                    quote_time,
                    received_at,
                    self.code,
                )
            except (ValueError, InvalidOperation):
                row_failures[symbol] = ProviderItemFailure(
                    symbol,
                    "PROVIDER_ITEM_INVALID",
                    "该股票行情字段无效",
                    self.code,
                )
        items = tuple(parsed[symbol] for symbol in symbols if symbol in parsed)
        failures = tuple(
            row_failures.get(symbol)
            or ProviderItemFailure(
                symbol, "PROVIDER_ITEM_MISSING", "上游未返回该股票", self.code
            )
            for symbol in symbols
            if symbol not in parsed
        )
        return ProviderBatchResult(items, failures)

    @staticmethod
    def parse_daily_bars(
        frame: Any, *, request: DailyBarRequest
    ) -> ProviderBatchResult[DailyBar]:
        required = {"open", "high", "low", "close", "volume", "amount"}
        if (
            frame is None
            or not hasattr(frame, "columns")
            or not hasattr(frame, "iterrows")
        ):
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
        if not required.issubset({str(value) for value in frame.columns}):
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
        raw_rows: list[tuple[date, dict[str, Any]]] = []
        try:
            for index, row in frame.iterrows():
                trading_date = (
                    index.date()
                    if hasattr(index, "date")
                    else date.fromisoformat(str(index)[:10])
                )
                if request.start <= trading_date <= request.end:
                    raw_rows.append((trading_date, row))
        except (TypeError, ValueError) as error:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE") from error
        if not raw_rows:
            return ProviderBatchResult(
                failures=(
                    ProviderItemFailure(
                        request.symbol,
                        "PROVIDER_ITEM_MISSING",
                        "新浪未返回请求窗口内的历史日线",
                        ProviderCode.SINA,
                    ),
                )
            )
        raw_rows.sort(key=lambda item: item[0])
        if request.capability is ProviderCapability.HISTORICAL_DAILY_QFQ:
            first_positive = next(
                (
                    index
                    for index, (_, row) in enumerate(raw_rows)
                    if all(
                        Decimal(str(row[field])).is_finite()
                        and Decimal(str(row[field])) > 0
                        for field in ("open", "high", "low", "close")
                    )
                ),
                None,
            )
            if first_positive is None:
                raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
            raw_rows = raw_rows[first_positive:]
        bars: list[DailyBar] = []
        seen: set[date] = set()
        try:
            for trading_date, row in raw_rows:
                if trading_date in seen:
                    raise ValueError("duplicate date")
                seen.add(trading_date)
                bars.append(
                    DailyBar(
                        symbol=request.symbol,
                        trading_date=trading_date,
                        open=Decimal(str(row["open"])),
                        high=Decimal(str(row["high"])),
                        low=Decimal(str(row["low"])),
                        close=Decimal(str(row["close"])),
                        volume=int(row["volume"]),
                        amount=Decimal(str(row["amount"])),
                        source=ProviderCode.SINA,
                        capability=request.capability,
                    )
                )
        except (InvalidOperation, KeyError, TypeError, ValueError) as error:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE") from error
        return ProviderBatchResult(tuple(bars))

    @staticmethod
    def parse_security_master_page(
        text: str, *, observed_at: datetime
    ) -> tuple[SecurityMasterRecord, ...]:
        try:
            rows = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE") from error
        if not isinstance(rows, list) or not rows:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")

        records: list[SecurityMasterRecord] = []
        seen: set[str] = set()
        try:
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError
                raw_symbol = str(row["symbol"]).lower()
                code = str(row["code"])
                name = str(row["name"]).strip()
                prefix = raw_symbol[:2]
                if (
                    prefix not in {"sh", "sz", "bj"}
                    or raw_symbol[2:] != code
                    or not name
                ):
                    raise ValueError
                symbol = f"{code}.{prefix.upper()}"
                if symbol in seen:
                    raise ValueError
                seen.add(symbol)
                records.append(
                    SecurityMasterRecord(
                        symbol=symbol,
                        name=name,
                        market=prefix.upper(),
                        security_type="A_SHARE",
                        listed_on=None,
                        delisted_on=None,
                        listed=None,
                        is_st="ST" in name.upper(),
                        suspended=None,
                        source=ProviderCode.SINA,
                        observed_at=observed_at,
                    )
                )
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE") from error
        return tuple(records)


def _sina_symbol(symbol: str) -> str:
    return f"{symbol[-2:].lower()}{symbol[:6]}"


def _load_sina_history(**kwargs: str) -> Any:
    import akshare as ak

    return ak.stock_zh_a_daily(**kwargs)
