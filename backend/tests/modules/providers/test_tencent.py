import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from long_invest.modules.providers.contracts import (
    MarketDailyGroupRequest,
    ProviderCapability,
    ProviderCode,
)
from long_invest.modules.providers.tencent import TencentRealtimeProvider


def test_tencent_parses_batch_quotes_and_preserves_source() -> None:
    received_at = datetime(2026, 7, 31, 8, 15, tzinfo=UTC)
    fields = [""] * 38
    fields[1] = "浦发银行"
    fields[2] = "600000"
    fields[3] = "9.51"
    fields[4] = "9.71"
    fields[5] = "9.59"
    fields[6] = "1382638"
    fields[30] = "20260731161454"
    fields[33] = "9.59"
    fields[34] = "9.28"
    fields[35] = "9.51/1382638/1299502653"
    fields[37] = "129950.2653"

    result = TencentRealtimeProvider.parse_quotes(
        f'v_sh600000="{"~".join(fields)}";',
        ("600000.SH", "000001.SZ"),
        received_at=received_at,
    )

    assert len(result.items) == 1
    assert result.items[0].source is ProviderCode.TENCENT
    assert result.items[0].price == Decimal("9.51")
    assert result.items[0].volume == 138_263_800
    assert result.items[0].amount == Decimal("1299502653")
    assert result.failures[0].symbol == "000001.SZ"


def test_tencent_requests_all_symbols_in_one_call() -> None:
    class Client:
        request = None

        async def request_text(
            self, request, *, deadline, encoding, allowed_content_types
        ):
            self.request = request
            assert deadline > datetime.now(UTC)
            assert encoding == "gb18030"
            assert allowed_content_types == frozenset({"text/plain", "text/html"})
            fields = [""] * 38
            for index, value in {
                3: "10",
                4: "9.9",
                5: "9.8",
                6: "1",
                30: "20260731100000",
                33: "10.1",
                34: "9.7",
                37: "0.1",
            }.items():
                fields[index] = value
            return f'v_sh600000="{"~".join(fields)}";'

    client = Client()
    result = asyncio.run(
        TencentRealtimeProvider(client).realtime_quotes(
            ("600000.SH",), datetime.now(UTC) + timedelta(seconds=5)
        )
    )

    assert len(result.items) == 1
    assert client.request.url.endswith("q=sh600000")
    assert client.request.headers["Referer"] == "https://gu.qq.com/"


def test_tencent_builds_current_day_daily_bar_from_batch_quote() -> None:
    class Client:
        async def request_text(self, request, **kwargs):
            del request, kwargs
            fields = [""] * 38
            for index, value in {
                3: "9.51",
                4: "9.71",
                5: "9.59",
                6: "1382638",
                30: "20260731161454",
                33: "9.59",
                34: "9.28",
                35: "9.51/1382638/1299502653",
                37: "129950.2653",
            }.items():
                fields[index] = value
            return f'v_sh600000="{"~".join(fields)}";'

    result = asyncio.run(
        TencentRealtimeProvider(Client()).market_daily_bars(
            MarketDailyGroupRequest(date(2026, 7, 31), ("600000.SH",), 0),
            datetime.now(UTC) + timedelta(seconds=5),
        )
    )

    assert len(result.items) == 1
    assert result.items[0].capability is ProviderCapability.DAILY_BAR_UNADJUSTED
    assert result.items[0].close == Decimal("9.51")
    assert result.items[0].source is ProviderCode.TENCENT


def test_tencent_rejects_quote_from_a_different_trading_day() -> None:
    class Client:
        async def request_text(self, request, **kwargs):
            del request, kwargs
            fields = [""] * 38
            for index, value in {
                3: "9.51",
                4: "9.71",
                5: "9.59",
                6: "1",
                30: "20260730161454",
                33: "9.59",
                34: "9.28",
                37: "0.1",
            }.items():
                fields[index] = value
            return f'v_sh600000="{"~".join(fields)}";'

    result = asyncio.run(
        TencentRealtimeProvider(Client()).market_daily_bars(
            MarketDailyGroupRequest(date(2026, 7, 31), ("600000.SH",), 0),
            datetime.now(UTC) + timedelta(seconds=5),
        )
    )

    assert not result.items
    assert result.failures[0].code == "PROVIDER_ITEM_STALE"
