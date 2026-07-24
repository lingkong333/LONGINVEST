from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Any, Protocol
from urllib.parse import urlsplit

from curl_cffi import CurlOpt
from curl_cffi.requests import Session
from curl_cffi.requests.exceptions import (
    CertificateVerifyError,
    RequestException,
)

from long_invest.modules.providers.http_client import (
    JSON_CONTENT_TYPES,
    RETRYABLE_STATUSES,
    ProviderHttpRequest,
)
from long_invest.modules.providers.retry import ProviderHttpError, run_with_retry


class BrowserSession(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any: ...

    def close(self) -> None: ...


class BrowserProviderHttpClient:
    def __init__(
        self,
        sessions: Iterable[BrowserSession],
        *,
        allowed_hosts: frozenset[str],
        max_response_bytes: int = 2_000_000,
        max_header_bytes: int = 16_384,
    ) -> None:
        self._sessions = tuple(sessions)
        if not self._sessions:
            raise ValueError("at least one browser session is required")
        self._allowed_hosts = allowed_hosts
        self._max_response_bytes = max_response_bytes
        self._max_header_bytes = max_header_bytes
        self._next_session_index = 0

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
            session = self._next_session()
            try:
                response = await asyncio.to_thread(
                    session.get,
                    request.url,
                    params=request.params,
                    headers=request.headers,
                    timeout=remaining,
                    allow_redirects=False,
                )
            except CertificateVerifyError as error:
                raise ProviderHttpError("PROVIDER_UPSTREAM_ERROR") from error
            except RequestException as error:
                raise ProviderHttpError(
                    "PROVIDER_UPSTREAM_TEMPORARY", retryable=True
                ) from error
            if response.status_code in RETRYABLE_STATUSES:
                raise ProviderHttpError(
                    "PROVIDER_UPSTREAM_TEMPORARY", retryable=True
                )
            if response.status_code >= 400:
                raise ProviderHttpError("PROVIDER_UPSTREAM_ERROR")
            body = bytes(response.content)
            self._validate_response(response.headers, body)
            return self._decode(body)

        return await run_with_retry(perform, deadline=deadline)

    async def close(self) -> None:
        await asyncio.gather(
            *(asyncio.to_thread(session.close) for session in self._sessions)
        )

    def _next_session(self) -> BrowserSession:
        session = self._sessions[self._next_session_index]
        self._next_session_index = (self._next_session_index + 1) % len(
            self._sessions
        )
        return session

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


def create_browser_json_client(
    *,
    host: str,
    resolve_addresses: Iterable[str],
    max_response_bytes: int = 2_000_000,
) -> BrowserProviderHttpClient:
    addresses = tuple(dict.fromkeys(resolve_addresses))
    for address in addresses:
        ip_address(address)
    sessions = tuple(
        Session(
            impersonate="chrome",
            default_headers=False,
            curl_options={CurlOpt.RESOLVE: [f"{host}:443:{address}"]},
        )
        for address in addresses
    )
    if not sessions:
        sessions = (Session(impersonate="chrome", default_headers=False),)
    return BrowserProviderHttpClient(
        sessions,
        allowed_hosts=frozenset({host}),
        max_response_bytes=max_response_bytes,
    )
