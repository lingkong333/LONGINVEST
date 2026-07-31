import asyncio
from datetime import UTC, datetime, timedelta
from functools import wraps

import httpx
import pytest

from long_invest.modules.providers.http_client import (
    ProviderHttpClient,
    ProviderHttpRequest,
)
from long_invest.modules.providers.retry import ProviderHttpError


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


@async_test
async def test_client_reuses_async_client_and_accepts_bounded_json() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, headers={"content-type": "application/json"}, json={"ok": True}
        )

    async_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False, verify=True
    )
    client = ProviderHttpClient(
        async_client,
        allowed_hosts=frozenset({"push2.example.test"}),
        max_response_bytes=100,
    )
    result = await client.request_json(
        ProviderHttpRequest(
            "https://push2.example.test/api", params={"secret": "hidden"}
        ),
        deadline=datetime.now(UTC) + timedelta(seconds=2),
    )
    await async_client.aclose()
    assert result == {"ok": True}
    assert len(seen) == 1


@async_test
async def test_each_retry_claims_and_releases_its_own_request_budget() -> None:
    attempts = 0
    entered = 0
    exited = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(502, headers={"content-type": "application/json"})
        return httpx.Response(
            200, headers={"content-type": "application/json"}, json={"ok": True}
        )

    class Guard:
        async def __aenter__(self):
            nonlocal entered
            entered += 1

        async def __aexit__(self, *args):
            nonlocal exited
            del args
            exited += 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as raw:
        client = ProviderHttpClient(
            raw,
            allowed_hosts=frozenset({"x.test"}),
            request_guard=Guard,
        )
        result = await client.request_json(
            ProviderHttpRequest("https://x.test/api"),
            deadline=datetime.now(UTC) + timedelta(seconds=2),
        )

    assert result == {"ok": True}
    assert (attempts, entered, exited) == (2, 2, 2)


@async_test
@pytest.mark.parametrize(
    "url", ["http://push2.example.test/api", "https://evil.test/api"]
)
async def test_client_rejects_non_tls_or_unapproved_host(url: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as raw:
        client = ProviderHttpClient(
            raw, allowed_hosts=frozenset({"push2.example.test"})
        )
        with pytest.raises(ProviderHttpError, match="PROVIDER_TARGET_NOT_ALLOWED"):
            await client.request_json(
                ProviderHttpRequest(url),
                deadline=datetime.now(UTC) + timedelta(seconds=1),
            )


@async_test
@pytest.mark.parametrize(
    "body,content_type,max_size,code",
    [
        (b"<html>login</html>", "text/html", 100, "PROVIDER_UNEXPECTED_CONTENT"),
        (b'{"captcha":"verify"}', "application/json", 100, "PROVIDER_CAPTCHA_DETECTED"),
        (
            b'{"long":"0123456789"}',
            "application/json",
            15,
            "PROVIDER_RESPONSE_TOO_LARGE",
        ),
    ],
)
async def test_client_rejects_html_captcha_and_oversize(
    body: bytes, content_type: str, max_size: int, code: str
) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, headers={"content-type": content_type}, content=body
        )
    )
    async with httpx.AsyncClient(transport=transport) as raw:
        client = ProviderHttpClient(
            raw, allowed_hosts=frozenset({"x.test"}), max_response_bytes=max_size
        )
        with pytest.raises(ProviderHttpError, match=code):
            await client.request_json(
                ProviderHttpRequest("https://x.test/api"),
                deadline=datetime.now(UTC) + timedelta(seconds=1),
            )


@async_test
async def test_client_stops_when_total_deadline_has_expired() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as raw:
        client = ProviderHttpClient(raw, allowed_hosts=frozenset({"x.test"}))
        with pytest.raises(ProviderHttpError, match="PROVIDER_TIMEOUT"):
            await client.request_json(
                ProviderHttpRequest("https://x.test/api"),
                deadline=datetime.now(UTC) - timedelta(seconds=1),
            )


@async_test
async def test_client_accepts_bounded_plain_text_for_sina() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/javascript; charset=GB18030"},
            content='var hq_str_sh600000="浦发银行,10";'.encode("gb18030"),
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as raw:
        client = ProviderHttpClient(raw, allowed_hosts=frozenset({"hq.sinajs.cn"}))
        text = await client.request_text(
            ProviderHttpRequest(
                "https://hq.sinajs.cn/list=sh600000",
                headers={"Referer": "https://finance.sina.com.cn/"},
            ),
            deadline=datetime.now(UTC) + timedelta(seconds=1),
            encoding="gb18030",
        )
    assert "浦发银行" in text
    assert seen[0].headers["referer"] == "https://finance.sina.com.cn/"


@async_test
async def test_declared_html_type_still_rejects_html_body() -> None:
    responses = iter(
        (
            httpx.Response(
                200,
                headers={"content-type": "text/html; charset=GBK"},
                content=b'v_sh600000="quote";',
            ),
            httpx.Response(
                200,
                headers={"content-type": "text/html; charset=GBK"},
                content=b"<html>blocked</html>",
            ),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: next(responses))
    ) as raw:
        client = ProviderHttpClient(raw, allowed_hosts=frozenset({"qt.gtimg.cn"}))
        text = await client.request_text(
            ProviderHttpRequest("https://qt.gtimg.cn/q=sh600000"),
            deadline=datetime.now(UTC) + timedelta(seconds=1),
            allowed_content_types=frozenset({"text/html"}),
        )
        assert text == 'v_sh600000="quote";'
        with pytest.raises(ProviderHttpError, match="PROVIDER_UNEXPECTED_CONTENT"):
            await client.request_text(
                ProviderHttpRequest("https://qt.gtimg.cn/q=sh600000"),
                deadline=datetime.now(UTC) + timedelta(seconds=1),
                allowed_content_types=frozenset({"text/html"}),
            )
