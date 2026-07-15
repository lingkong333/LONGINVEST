from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from long_invest.modules.providers.retry import ProviderHttpError, run_with_retry

RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
JSON_CONTENT_TYPES = frozenset({"application/json", "text/json", "text/plain"})


@dataclass(frozen=True, slots=True)
class ProviderHttpRequest:
    url: str
    params: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


def create_async_client(
    *,
    connect_timeout: float = 3,
    read_timeout: float = 5,
    write_timeout: float = 3,
    pool_timeout: float = 1,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=write_timeout,
            pool=pool_timeout,
        ),
        follow_redirects=False,
        verify=True,
    )


class ProviderHttpClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        allowed_hosts: frozenset[str],
        max_response_bytes: int = 2_000_000,
        max_header_bytes: int = 16_384,
    ) -> None:
        self._client = client
        self._allowed_hosts = allowed_hosts
        self._max_response_bytes = max_response_bytes
        self._max_header_bytes = max_header_bytes

    async def request_json(
        self, request: ProviderHttpRequest, *, deadline: datetime
    ) -> dict[str, Any]:
        self._validate_target(request.url)
        if deadline.tzinfo is None:
            raise ValueError("deadline must include timezone")

        async def perform() -> dict[str, Any]:
            remaining = (deadline - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                raise ProviderHttpError("PROVIDER_TIMEOUT")
            try:
                async with asyncio.timeout(remaining):
                    async with self._client.stream(
                        "GET",
                        request.url,
                        params=request.params,
                        headers=request.headers,
                    ) as response:
                        if response.status_code in RETRYABLE_STATUSES:
                            raise ProviderHttpError(
                                "PROVIDER_UPSTREAM_TEMPORARY", retryable=True
                            )
                        if response.status_code >= 400:
                            raise ProviderHttpError("PROVIDER_UPSTREAM_ERROR")
                        self._validate_headers(response, JSON_CONTENT_TYPES)
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > self._max_response_bytes:
                                raise ProviderHttpError("PROVIDER_RESPONSE_TOO_LARGE")
            except TimeoutError as error:
                raise ProviderHttpError("PROVIDER_TIMEOUT", retryable=True) from error
            return self._decode(bytes(body))

        return await run_with_retry(perform, deadline=deadline)

    async def request_text(
        self,
        request: ProviderHttpRequest,
        *,
        deadline: datetime,
        encoding: str = "utf-8",
    ) -> str:
        self._validate_target(request.url)

        async def perform() -> str:
            remaining = (deadline - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                raise ProviderHttpError("PROVIDER_TIMEOUT")
            try:
                async with asyncio.timeout(remaining):
                    async with self._client.stream(
                        "GET",
                        request.url,
                        params=request.params,
                        headers=request.headers,
                    ) as response:
                        if response.status_code in RETRYABLE_STATUSES:
                            raise ProviderHttpError(
                                "PROVIDER_UPSTREAM_TEMPORARY", retryable=True
                            )
                        if response.status_code >= 400:
                            raise ProviderHttpError("PROVIDER_UPSTREAM_ERROR")
                        self._validate_headers(
                            response,
                            frozenset(
                                {
                                    "text/plain",
                                    "application/javascript",
                                    "text/javascript",
                                }
                            ),
                        )
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > self._max_response_bytes:
                                raise ProviderHttpError("PROVIDER_RESPONSE_TOO_LARGE")
            except TimeoutError as error:
                raise ProviderHttpError("PROVIDER_TIMEOUT", retryable=True) from error
            lowered = bytes(body).lower()
            if b"<html" in lowered:
                raise ProviderHttpError("PROVIDER_UNEXPECTED_CONTENT")
            if any(marker in lowered for marker in (b"captcha", b"validatecode")):
                raise ProviderHttpError("PROVIDER_CAPTCHA_DETECTED")
            try:
                return bytes(body).decode(encoding)
            except (LookupError, UnicodeDecodeError) as error:
                raise ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE") from error

        return await run_with_retry(perform, deadline=deadline)

    def _validate_target(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in self._allowed_hosts:
            raise ProviderHttpError("PROVIDER_TARGET_NOT_ALLOWED")
        if parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ProviderHttpError("PROVIDER_TARGET_NOT_ALLOWED")

    def _validate_headers(
        self, response: httpx.Response, allowed_content_types: frozenset[str]
    ) -> None:
        if (
            sum(len(k) + len(v) for k, v in response.headers.items())
            > self._max_header_bytes
        ):
            raise ProviderHttpError("PROVIDER_RESPONSE_TOO_LARGE")
        length = response.headers.get("content-length")
        if length and int(length) > self._max_response_bytes:
            raise ProviderHttpError("PROVIDER_RESPONSE_TOO_LARGE")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type not in allowed_content_types:
            raise ProviderHttpError("PROVIDER_UNEXPECTED_CONTENT")

    def _decode(self, body: bytes) -> dict[str, Any]:
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
