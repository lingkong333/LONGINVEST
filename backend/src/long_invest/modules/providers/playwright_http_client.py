from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

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
    def __init__(self, *, allowed_hosts: frozenset[str]) -> None:
        self._allowed_hosts = allowed_hosts
        self._runtime: Any | None = None
        self._browser: Any | None = None
        self._start_lock = asyncio.Lock()

    async def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_ms: float,
    ) -> BrowserResponse:
        browser = await self._get_browser()
        page = await browser.new_page(extra_http_headers=dict(headers))

        async def guard_request(route: Any) -> None:
            request = route.request
            if request.redirected_from is not None or not self._is_allowed(request.url):
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        await page.route("**/*", guard_request)
        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError

            try:
                response = await page.goto(
                    url,
                    wait_until="commit",
                    timeout=timeout_ms,
                )
                if response is None:
                    raise ProviderHttpError(
                        "PROVIDER_UPSTREAM_TEMPORARY", retryable=True
                    )
                return BrowserResponse(
                    status_code=response.status,
                    headers=response.headers,
                    body=await response.body(),
                    url=response.url,
                )
            except PlaywrightTimeoutError as error:
                raise ProviderHttpError(
                    "PROVIDER_TIMEOUT", retryable=True
                ) from error
            except PlaywrightError as error:
                raise ProviderHttpError(
                    "PROVIDER_UPSTREAM_TEMPORARY", retryable=True
                ) from error
        finally:
            await page.close()

    async def close(self) -> None:
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
                except ImportError as error:
                    raise RuntimeError(
                        "Playwright history transport is not installed"
                    ) from error
                self._runtime = await async_playwright().start()
                self._browser = await self._runtime.chromium.launch(headless=True)
        return self._browser

    def _is_allowed(self, url: str) -> bool:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in self._allowed_hosts
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
        )


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
        self._last_started_at: float | None = None

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
                self._last_started_at = monotonic()
                response = await self._fetcher.fetch(
                    url,
                    headers=request.headers,
                    timeout_ms=remaining * 1000,
                )
            if response.status_code in RETRYABLE_STATUSES:
                raise ProviderHttpError(
                    "PROVIDER_UPSTREAM_TEMPORARY", retryable=True
                )
            if response.status_code >= 400:
                raise ProviderHttpError("PROVIDER_UPSTREAM_ERROR")
            self._validate_target(response.url)
            self._validate_response(response.headers, response.body)
            return self._decode(response.body)

        return await run_with_retry(perform, deadline=deadline)

    async def close(self) -> None:
        await self._fetcher.close()

    async def _wait_for_turn(self, deadline: datetime) -> None:
        if self._last_started_at is None:
            return
        delay = self._minimum_interval_seconds - (
            monotonic() - self._last_started_at
        )
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
    minimum_interval_seconds: float = 3,
    max_response_bytes: int = 2_000_000,
) -> PlaywrightProviderHttpClient:
    allowed_hosts = frozenset({host})
    return PlaywrightProviderHttpClient(
        PlaywrightPageFetcher(allowed_hosts=allowed_hosts),
        allowed_hosts=allowed_hosts,
        minimum_interval_seconds=minimum_interval_seconds,
        max_response_bytes=max_response_bytes,
    )
