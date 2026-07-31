import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from long_invest.modules.providers.contracts import ProviderCode
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
