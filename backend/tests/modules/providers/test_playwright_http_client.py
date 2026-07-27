import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from long_invest.modules.providers.http_client import ProviderHttpRequest
from long_invest.modules.providers.playwright_http_client import (
    BrowserResponse,
    PlaywrightProviderHttpClient,
    _activate_history_request,
    _rewrite_page_history_url,
    _unwrap_jsonp,
    _validated_quote_url,
    create_playwright_json_client,
)
from long_invest.modules.providers.retry import ProviderHttpError


class FakeFetcher:
    def __init__(self, *responses: BrowserResponse) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str], float]] = []
        self.closed = False

    async def fetch(self, url: str, *, headers, timeout_ms: float):
        self.calls.append((url, dict(headers), timeout_ms))
        return self.responses.pop(0)

    async def close(self) -> None:
        self.closed = True


def response(
    body: bytes = b'{"ok":true}', *, status_code: int = 200
) -> BrowserResponse:
    return BrowserResponse(
        status_code=status_code,
        headers={
            "content-type": "application/json",
            "content-length": str(len(body)),
        },
        body=body,
        url="https://push2his.eastmoney.com/api/qt/stock/kline/get",
    )


def test_playwright_client_builds_query_and_returns_json() -> None:
    fetcher = FakeFetcher(response())
    client = PlaywrightProviderHttpClient(
        fetcher,
        allowed_hosts=frozenset({"push2his.eastmoney.com"}),
        minimum_interval_seconds=0,
    )

    result = asyncio.run(
        client.request_json(
            ProviderHttpRequest(
                "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                params={"secid": "0.000002", "fields2": "f51,f52"},
                headers={"Referer": "https://quote.eastmoney.com/"},
            ),
            deadline=datetime.now(UTC) + timedelta(seconds=2),
        )
    )

    assert result == {"ok": True}
    assert "secid=0.000002" in fetcher.calls[0][0]
    assert "fields2=f51,f52" in fetcher.calls[0][0]
    assert fetcher.calls[0][1]["Referer"].startswith("https://quote")
    asyncio.run(client.close())
    assert fetcher.closed is True


def test_playwright_client_retries_temporary_status() -> None:
    fetcher = FakeFetcher(response(status_code=503), response())
    client = PlaywrightProviderHttpClient(
        fetcher,
        allowed_hosts=frozenset({"push2his.eastmoney.com"}),
        minimum_interval_seconds=0,
    )

    result = asyncio.run(
        client.request_json(
            ProviderHttpRequest("https://push2his.eastmoney.com/api"),
            deadline=datetime.now(UTC) + timedelta(seconds=2),
        )
    )

    assert result == {"ok": True}
    assert len(fetcher.calls) == 2


@pytest.mark.parametrize(
    "url,body,content_type,max_size,code",
    [
        (
            "https://evil.test/api",
            b"{}",
            "application/json",
            100,
            "PROVIDER_TARGET_NOT_ALLOWED",
        ),
        (
            "https://push2his.eastmoney.com/api",
            b"<html>blocked</html>",
            "text/html",
            100,
            "PROVIDER_UNEXPECTED_CONTENT",
        ),
        (
            "https://push2his.eastmoney.com/api",
            b'{"captcha":"verify"}',
            "application/json",
            10,
            "PROVIDER_RESPONSE_TOO_LARGE",
        ),
    ],
)
def test_playwright_client_keeps_response_guards(
    url: str,
    body: bytes,
    content_type: str,
    max_size: int,
    code: str,
) -> None:
    guarded_response = BrowserResponse(
        status_code=200,
        headers={
            "content-type": content_type,
            "content-length": str(len(body)),
        },
        body=body,
        url="https://push2his.eastmoney.com/api",
    )
    fetcher = FakeFetcher(guarded_response)
    client = PlaywrightProviderHttpClient(
        fetcher,
        allowed_hosts=frozenset({"push2his.eastmoney.com"}),
        minimum_interval_seconds=0,
        max_response_bytes=max_size,
    )

    with pytest.raises(ProviderHttpError, match=code):
        asyncio.run(
            client.request_json(
                ProviderHttpRequest(url),
                deadline=datetime.now(UTC) + timedelta(seconds=1),
            )
        )


def test_playwright_client_rejects_invalid_configured_address() -> None:
    with pytest.raises(ValueError):
        create_playwright_json_client(
            host="push2his.eastmoney.com",
            resolve_addresses=("not-an-ip",),
        )


def test_quote_page_rewrite_keeps_page_callback_and_target_year() -> None:
    rewritten = _rewrite_page_history_url(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        "cb=pageCallback&secid=0.000001&fqt=1&beg=0&end=20500101&_=123",
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        "secid=0.000001&fqt=0&beg=20250101&end=20251231&smplmt=999",
    )

    assert "cb=pageCallback" in rewritten
    assert "fqt=0" in rewritten
    assert "beg=20250101" in rewritten
    assert "end=20251231" in rewritten
    assert "smplmt=460" in rewritten
    assert "_=123" in rewritten


def test_quote_page_requires_exact_official_symbol_url() -> None:
    assert (
        _validated_quote_url({"Referer": "https://quote.eastmoney.com/sh600519.html"})
        == "https://quote.eastmoney.com/sh600519.html"
    )

    with pytest.raises(ProviderHttpError, match="PROVIDER_TARGET_NOT_ALLOWED"):
        _validated_quote_url(
            {"Referer": "https://quote.eastmoney.com/redirect?symbol=600519"}
        )


def test_quote_page_jsonp_is_normalized_to_json() -> None:
    assert _unwrap_jsonp(b'callback123({"rc":0});\n') == b'{"rc":0}'

    with pytest.raises(ProviderHttpError, match="PROVIDER_SCHEMA_INCOMPATIBLE"):
        _unwrap_jsonp(b'callback-name({"rc":0})')


@pytest.mark.anyio
async def test_activate_history_request_switches_weekly_then_daily() -> None:
    class Tabs:
        def __init__(self) -> None:
            self.clicked: list[tuple[int, bool, float]] = []
            self.index = 0

        async def all_inner_texts(self) -> list[str]:
            return ["日K", "周K", "月K", "5分钟"]

        def nth(self, index: int) -> "Tabs":
            self.index = index
            return self

        async def click(self, *, force: bool, timeout: float) -> None:
            self.clicked.append((self.index, force, timeout))

    class Page:
        def __init__(self, tabs: Tabs) -> None:
            self.tabs = tabs

        def locator(self, selector: str) -> Tabs:
            assert selector == "ul.k_tab a"
            return self.tabs

    tabs = Tabs()
    await _activate_history_request(Page(tabs), timeout_ms=60_000)

    assert tabs.clicked == [(1, True, 15_000), (0, True, 15_000)]


@pytest.mark.anyio
async def test_activate_history_request_rejects_changed_page_schema() -> None:
    class Tabs:
        async def all_inner_texts(self) -> list[str]:
            return ["分时", "五日"]

    class Page:
        def locator(self, selector: str) -> Tabs:
            assert selector == "ul.k_tab a"
            return Tabs()

    with pytest.raises(ProviderHttpError, match="PROVIDER_SCHEMA_INCOMPATIBLE"):
        await _activate_history_request(Page(), timeout_ms=60_000)
