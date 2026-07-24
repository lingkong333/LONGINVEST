import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from curl_cffi.requests.exceptions import ConnectionError

from long_invest.modules.providers.browser_http_client import (
    BrowserProviderHttpClient,
    create_browser_json_client,
)
from long_invest.modules.providers.http_client import ProviderHttpRequest
from long_invest.modules.providers.retry import ProviderHttpError


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status_code: int = 200,
        content_type: str = "application/json",
    ) -> None:
        self.status_code = status_code
        self.content = body
        self.headers = {
            "content-type": content_type,
            "content-length": str(len(body)),
        }


class FakeSession:
    def __init__(self, result) -> None:
        self.result = result
        self.calls = []
        self.closed = False

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self) -> None:
        self.closed = True


def test_browser_client_rotates_official_endpoints_after_temporary_failure() -> None:
    failed = FakeSession(ConnectionError("connection closed"))
    healthy = FakeSession(FakeResponse(b'{"ok":true}'))
    client = BrowserProviderHttpClient(
        (failed, healthy), allowed_hosts=frozenset({"push2his.eastmoney.com"})
    )

    result = asyncio.run(
        client.request_json(
            ProviderHttpRequest(
                "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                params={"secid": "0.000001"},
            ),
            deadline=datetime.now(UTC) + timedelta(seconds=2),
        )
    )

    assert result == {"ok": True}
    assert len(failed.calls) == len(healthy.calls) == 1
    assert healthy.calls[0][1]["allow_redirects"] is False
    asyncio.run(client.close())
    assert failed.closed and healthy.closed


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
def test_browser_client_keeps_provider_response_guards(
    url: str,
    body: bytes,
    content_type: str,
    max_size: int,
    code: str,
) -> None:
    client = BrowserProviderHttpClient(
        (FakeSession(FakeResponse(body, content_type=content_type)),),
        allowed_hosts=frozenset({"push2his.eastmoney.com"}),
        max_response_bytes=max_size,
    )

    with pytest.raises(ProviderHttpError, match=code):
        asyncio.run(
            client.request_json(
                ProviderHttpRequest(url),
                deadline=datetime.now(UTC) + timedelta(seconds=1),
            )
        )


def test_browser_client_rejects_invalid_configured_address() -> None:
    with pytest.raises(ValueError):
        create_browser_json_client(
            host="push2his.eastmoney.com",
            resolve_addresses=("not-an-ip",),
        )
