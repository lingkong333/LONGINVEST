import asyncio
from datetime import datetime, timedelta, timezone
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
        return httpx.Response(200, headers={"content-type": "application/json"}, json={"ok": True})

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False, verify=True)
    client = ProviderHttpClient(async_client, allowed_hosts=frozenset({"push2.example.test"}), max_response_bytes=100)
    result = await client.request_json(
        ProviderHttpRequest("https://push2.example.test/api", params={"secret": "hidden"}),
        deadline=datetime.now(timezone.utc) + timedelta(seconds=2),
    )
    await async_client.aclose()
    assert result == {"ok": True}
    assert len(seen) == 1


@async_test
@pytest.mark.parametrize("url", ["http://push2.example.test/api", "https://evil.test/api"])
async def test_client_rejects_non_tls_or_unapproved_host(url: str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as raw:
        client = ProviderHttpClient(raw, allowed_hosts=frozenset({"push2.example.test"}))
        with pytest.raises(ProviderHttpError, match="PROVIDER_TARGET_NOT_ALLOWED"):
            await client.request_json(ProviderHttpRequest(url), deadline=datetime.now(timezone.utc) + timedelta(seconds=1))


@async_test
@pytest.mark.parametrize("body,content_type,max_size,code", [
    (b"<html>login</html>", "text/html", 100, "PROVIDER_UNEXPECTED_CONTENT"),
    (b'{"captcha":"verify"}', "application/json", 100, "PROVIDER_CAPTCHA_DETECTED"),
    (b'{"long":"0123456789"}', "application/json", 15, "PROVIDER_RESPONSE_TOO_LARGE"),
])
async def test_client_rejects_html_captcha_and_oversize(
    body: bytes, content_type: str, max_size: int, code: str
) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, headers={"content-type": content_type}, content=body))
    async with httpx.AsyncClient(transport=transport) as raw:
        client = ProviderHttpClient(raw, allowed_hosts=frozenset({"x.test"}), max_response_bytes=max_size)
        with pytest.raises(ProviderHttpError, match=code):
            await client.request_json(ProviderHttpRequest("https://x.test/api"), deadline=datetime.now(timezone.utc) + timedelta(seconds=1))


@async_test
async def test_client_stops_when_total_deadline_has_expired() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as raw:
        client = ProviderHttpClient(raw, allowed_hosts=frozenset({"x.test"}))
        with pytest.raises(ProviderHttpError, match="PROVIDER_TIMEOUT"):
            await client.request_json(ProviderHttpRequest("https://x.test/api"), deadline=datetime.now(timezone.utc) - timedelta(seconds=1))
