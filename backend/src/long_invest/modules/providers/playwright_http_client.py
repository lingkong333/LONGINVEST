from __future__ import annotations

import asyncio
import json
import re
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import ip_address
from time import monotonic
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from long_invest.modules.providers.http_client import (
    JSON_CONTENT_TYPES,
    RETRYABLE_STATUSES,
    ProviderHttpRequest,
)
from long_invest.modules.providers.retry import ProviderHttpError, run_with_retry


@dataclass(frozen=True, slots=True)
class BrowserResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    url: str


class BrowserFetcher(Protocol):
    async def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_ms: float,
    ) -> BrowserResponse: ...

    async def close(self) -> None: ...


class PlaywrightPageFetcher:
    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str],
        resolve_addresses: tuple[str, ...] = (),
        max_sessions: int = 8,
    ) -> None:
        if max_sessions <= 0:
            raise ValueError("max_sessions must be positive")
        self._allowed_hosts = allowed_hosts
        self._resolve_addresses = resolve_addresses
        self._max_sessions = max_sessions
        self._address_index = 0
        self._runtime: Any | None = None
        self._browser: Any | None = None
        self._stealth: Any | None = None
        self._browser_major_version: str | None = None
        self._contexts: OrderedDict[str, Any] = OrderedDict()
        self._start_lock = asyncio.Lock()

    async def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_ms: float,
    ) -> BrowserResponse:
        quote_url = _validated_quote_url(headers)
        should_rotate = False
        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError

            try:
                context, is_new = await self._session(quote_url)
                if is_new:
                    try:
                        return await self._fetch_first_segment(
                            context,
                            quote_url=quote_url,
                            target_url=url,
                            timeout_ms=timeout_ms,
                        )
                    except Exception:
                        await self._drop_session(quote_url)
                        raise
                return await self._fetch_direct(
                    context,
                    quote_url=quote_url,
                    target_url=url,
                    timeout_ms=timeout_ms,
                )
            except PlaywrightTimeoutError as error:
                should_rotate = True
                raise ProviderHttpError("PROVIDER_TIMEOUT", retryable=True) from error
            except PlaywrightError as error:
                should_rotate = True
                raise ProviderHttpError(
                    "PROVIDER_UPSTREAM_TEMPORARY", retryable=True
                ) from error
        finally:
            if should_rotate:
                await self._rotate_browser()

    async def close(self) -> None:
        await self._close_contexts()
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._runtime is not None:
            await self._runtime.stop()
            self._runtime = None

    async def _get_browser(self) -> Any:
        if self._browser is not None:
            return self._browser
        async with self._start_lock:
            if self._browser is None:
                try:
                    from playwright.async_api import async_playwright
                    from playwright_stealth import Stealth
                except ImportError as error:
                    raise RuntimeError(
                        "Playwright collector dependencies are not installed"
                    ) from error
                self._runtime = await async_playwright().start()
                launch_args = [
                    "--disable-blink-features=AutomationControlled",
                    "--accept-lang=zh-CN,zh",
                    "--window-size=1628,805",
                ]
                if self._resolve_addresses:
                    address = self._resolve_addresses[self._address_index]
                    host = next(iter(self._allowed_hosts))
                    launch_args.append(f"--host-resolver-rules=MAP {host} {address}")
                browser = await self._runtime.chromium.launch(
                    channel="chrome",
                    headless=True,
                    args=launch_args,
                )
                major = browser.version.split(".", 1)[0]
                if not major.isdigit():
                    await browser.close()
                    raise RuntimeError("Google Chrome version is invalid")
                self._browser = browser
                self._browser_major_version = major
                self._stealth = Stealth(
                    navigator_languages_override=("zh-CN", "zh"),
                    navigator_platform_override="Win32",
                    navigator_user_agent_override=_windows_user_agent(major),
                    navigator_vendor_override="Google Inc.",
                    sec_ch_ua_override=_sec_ch_ua(major),
                    webgl_vendor_override="Google Inc. (NVIDIA)",
                    webgl_renderer_override=(
                        "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 "
                        "(0x00002487) Direct3D11 vs_5_0 ps_5_0, D3D11)"
                    ),
                )
        return self._browser

    async def _session(self, quote_url: str) -> tuple[Any, bool]:
        context = self._contexts.get(quote_url)
        if context is not None:
            self._contexts.move_to_end(quote_url)
            return context, False
        browser = await self._get_browser()
        if self._stealth is None or self._browser_major_version is None:
            raise RuntimeError("Google Chrome profile is not initialized")
        context = await browser.new_context(
            user_agent=_windows_user_agent(self._browser_major_version),
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1628, "height": 805},
            screen={"width": 2560, "height": 1440},
            device_scale_factor=1.5,
            color_scheme="light",
            extra_http_headers={
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Sec-CH-UA": _sec_ch_ua(self._browser_major_version),
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Platform": '"Windows"',
            },
        )
        await self._stealth.apply_stealth_async(context)
        await context.add_init_script(_DESKTOP_PROFILE_SCRIPT)
        self._contexts[quote_url] = context
        while len(self._contexts) > self._max_sessions:
            _, expired = self._contexts.popitem(last=False)
            with suppress(Exception):
                await expired.close()
        return context, True

    async def _fetch_first_segment(
        self,
        context: Any,
        *,
        quote_url: str,
        target_url: str,
        timeout_ms: float,
    ) -> BrowserResponse:
        page = await context.new_page()
        handled = False

        async def rewrite(route: Any, request: Any) -> None:
            nonlocal handled
            if not _is_history_url(request.url):
                await route.continue_()
                return
            if handled:
                await route.abort()
                return
            handled = True
            await route.continue_(
                url=_rewrite_page_history_url(request.url, target_url)
            )

        await page.route(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get**",
            rewrite,
        )
        try:
            async with page.expect_response(
                lambda response: _is_history_url(response.url),
                timeout=timeout_ms,
            ) as response_info:
                quote_response = await page.goto(
                    quote_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                if quote_response is None or quote_response.request.redirected_from:
                    raise ProviderHttpError("PROVIDER_UPSTREAM_ERROR")
                await _activate_history_request(page, timeout_ms=timeout_ms)
            response = await response_info.value
            body = _unwrap_jsonp(await response.body())
            response_headers = dict(response.headers)
            response_headers["content-type"] = "application/json"
            response_headers["content-length"] = str(len(body))
            return BrowserResponse(
                status_code=response.status,
                headers=response_headers,
                body=body,
                url=response.url,
            )
        finally:
            with suppress(Exception):
                await page.close()

    @staticmethod
    async def _fetch_direct(
        context: Any,
        *,
        quote_url: str,
        target_url: str,
        timeout_ms: float,
    ) -> BrowserResponse:
        page = await context.new_page()
        try:
            await page.set_extra_http_headers({"Referer": quote_url})
            response = await page.goto(
                target_url,
                wait_until="commit",
                timeout=timeout_ms,
            )
            if response is None:
                raise ProviderHttpError("PROVIDER_UPSTREAM_TEMPORARY", retryable=True)
            if response.request.redirected_from is not None:
                raise ProviderHttpError("PROVIDER_UPSTREAM_ERROR")
            return BrowserResponse(
                status_code=response.status,
                headers=response.headers,
                body=await response.body(),
                url=response.url,
            )
        finally:
            with suppress(Exception):
                await page.close()

    async def _drop_session(self, quote_url: str) -> None:
        context = self._contexts.pop(quote_url, None)
        if context is not None:
            with suppress(Exception):
                await context.close()

    async def _close_contexts(self) -> None:
        contexts = tuple(self._contexts.values())
        self._contexts.clear()
        for context in contexts:
            with suppress(Exception):
                await context.close()

    async def _rotate_browser(self) -> None:
        await self._close_contexts()
        if self._browser is not None:
            with suppress(Exception):
                await self._browser.close()
            self._browser = None
        self._stealth = None
        self._browser_major_version = None
        if self._resolve_addresses:
            self._address_index = (self._address_index + 1) % len(
                self._resolve_addresses
            )


_QUOTE_PATH = re.compile(r"/(?:sh|sz|bj)\d{6}\.html")
_HISTORY_PATH = "/api/qt/stock/kline/get"
_DESKTOP_PROFILE_SCRIPT = """
Object.defineProperty(
  Navigator.prototype,
  "hardwareConcurrency",
  { get: () => 16, configurable: true }
);
Object.defineProperty(
  Navigator.prototype,
  "deviceMemory",
  { get: () => 32, configurable: true }
);
"""


def _windows_user_agent(major: str) -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


def _sec_ch_ua(major: str) -> str:
    return f'"Not;A=Brand";v="8", "Chromium";v="{major}", "Google Chrome";v="{major}"'


def _validated_quote_url(headers: Mapping[str, str]) -> str:
    referer = next(
        (value for key, value in headers.items() if key.lower() == "referer"),
        "",
    )
    parsed = urlsplit(referer)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "quote.eastmoney.com"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or _QUOTE_PATH.fullmatch(parsed.path) is None
    ):
        raise ProviderHttpError("PROVIDER_TARGET_NOT_ALLOWED")
    return referer


def _is_history_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "push2his.eastmoney.com"
        and parsed.path == _HISTORY_PATH
    )


def _rewrite_page_history_url(page_url: str, target_url: str) -> str:
    page = urlsplit(page_url)
    target = urlsplit(target_url)
    if not _is_history_url(page_url) or not _is_history_url(target_url):
        raise ProviderHttpError("PROVIDER_TARGET_NOT_ALLOWED")
    page_params = dict(parse_qsl(page.query, keep_blank_values=True))
    target_params = dict(parse_qsl(target.query, keep_blank_values=True))
    for name in ("cb", "_"):
        if name in page_params:
            target_params[name] = page_params[name]
    target_params["smplmt"] = "460"
    return urlunsplit(
        (
            target.scheme,
            target.netloc,
            target.path,
            urlencode(target_params, safe=","),
            "",
        )
    )


def _unwrap_jsonp(body: bytes) -> bytes:
    stripped = body.strip()
    if stripped.startswith(b"{"):
        return stripped
    opening = stripped.find(b"(")
    closing = stripped.rfind(b")")
    callback = stripped[:opening]
    if (
        opening <= 0
        or closing <= opening
        or re.fullmatch(rb"[A-Za-z_$][A-Za-z0-9_$]*", callback) is None
    ):
        raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
    return stripped[opening + 1 : closing]


async def _activate_history_request(page: Any, *, timeout_ms: float) -> None:
    tabs = page.locator("ul.k_tab a")
    labels = tuple(label.strip() for label in await tabs.all_inner_texts())
    if labels[:3] != ("日K", "周K", "月K"):
        raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
    click_timeout = min(timeout_ms, 15_000)
    await tabs.nth(1).click(force=True, timeout=click_timeout)
    await tabs.nth(0).click(force=True, timeout=click_timeout)


class PlaywrightProviderHttpClient:
    def __init__(
        self,
        fetcher: BrowserFetcher,
        *,
        allowed_hosts: frozenset[str],
        minimum_interval_seconds: float = 3,
        max_response_bytes: int = 2_000_000,
        max_header_bytes: int = 16_384,
    ) -> None:
        self._fetcher = fetcher
        self._allowed_hosts = allowed_hosts
        self._minimum_interval_seconds = minimum_interval_seconds
        self._max_response_bytes = max_response_bytes
        self._max_header_bytes = max_header_bytes
        self._request_lock = asyncio.Lock()
        self._last_completed_at: float | None = None

    async def request_json(
        self, request: ProviderHttpRequest, *, deadline: datetime
    ) -> dict[str, Any]:
        self._validate_target(request.url)
        if deadline.tzinfo is None:
            raise ValueError("deadline must include timezone")
        url = self._build_url(request)

        async def perform() -> dict[str, Any]:
            async with self._request_lock:
                await self._wait_for_turn(deadline)
                remaining = (deadline - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    raise ProviderHttpError("PROVIDER_TIMEOUT")
                try:
                    response = await self._fetcher.fetch(
                        url,
                        headers=request.headers,
                        timeout_ms=remaining * 1000,
                    )
                finally:
                    self._last_completed_at = monotonic()
            if response.status_code in RETRYABLE_STATUSES:
                raise ProviderHttpError("PROVIDER_UPSTREAM_TEMPORARY", retryable=True)
            if response.status_code >= 400:
                raise ProviderHttpError("PROVIDER_UPSTREAM_ERROR")
            self._validate_target(response.url)
            self._validate_response(response.headers, response.body)
            return self._decode(response.body)

        return await run_with_retry(perform, deadline=deadline)

    async def close(self) -> None:
        await self._fetcher.close()

    async def _wait_for_turn(self, deadline: datetime) -> None:
        if self._last_completed_at is None:
            return
        delay = self._minimum_interval_seconds - (monotonic() - self._last_completed_at)
        if delay <= 0:
            return
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if delay >= remaining:
            raise ProviderHttpError("PROVIDER_TIMEOUT")
        await asyncio.sleep(delay)

    @staticmethod
    def _build_url(request: ProviderHttpRequest) -> str:
        parsed = urlsplit(request.url)
        query = urlencode(request.params, safe=",")
        if parsed.query and query:
            query = f"{parsed.query}&{query}"
        elif parsed.query:
            query = parsed.query
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))

    def _validate_target(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in self._allowed_hosts:
            raise ProviderHttpError("PROVIDER_TARGET_NOT_ALLOWED")
        if parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ProviderHttpError("PROVIDER_TARGET_NOT_ALLOWED")

    def _validate_response(self, headers: Mapping[str, str], body: bytes) -> None:
        if sum(len(k) + len(v) for k, v in headers.items()) > self._max_header_bytes:
            raise ProviderHttpError("PROVIDER_RESPONSE_TOO_LARGE")
        length = headers.get("content-length")
        if length:
            try:
                if int(length) > self._max_response_bytes:
                    raise ProviderHttpError("PROVIDER_RESPONSE_TOO_LARGE")
            except ValueError as error:
                raise ProviderHttpError("PROVIDER_UNEXPECTED_CONTENT") from error
        if len(body) > self._max_response_bytes:
            raise ProviderHttpError("PROVIDER_RESPONSE_TOO_LARGE")
        content_type = headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type not in JSON_CONTENT_TYPES:
            raise ProviderHttpError("PROVIDER_UNEXPECTED_CONTENT")

    @staticmethod
    def _decode(body: bytes) -> dict[str, Any]:
        lowered = body.lower()
        if any(
            marker in lowered
            for marker in (b"<html", b"captcha", b"verify", b"validatecode")
        ):
            code = (
                "PROVIDER_CAPTCHA_DETECTED"
                if b"<html" not in lowered
                else "PROVIDER_UNEXPECTED_CONTENT"
            )
            raise ProviderHttpError(code)
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE") from error
        if not isinstance(value, dict):
            raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE")
        return value


def create_playwright_json_client(
    *,
    host: str,
    resolve_addresses: tuple[str, ...] = (),
    minimum_interval_seconds: float = 3,
    max_response_bytes: int = 2_000_000,
) -> PlaywrightProviderHttpClient:
    allowed_hosts = frozenset({host})
    addresses = tuple(dict.fromkeys(resolve_addresses))
    for address in addresses:
        ip_address(address)
    return PlaywrightProviderHttpClient(
        PlaywrightPageFetcher(
            allowed_hosts=allowed_hosts,
            resolve_addresses=addresses,
        ),
        allowed_hosts=allowed_hosts,
        minimum_interval_seconds=minimum_interval_seconds,
        max_response_bytes=max_response_bytes,
    )
