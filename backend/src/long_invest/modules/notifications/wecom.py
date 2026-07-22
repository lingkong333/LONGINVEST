import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from long_invest.modules.notifications.channels import (
    ChannelResult,
    ChannelSendRequest,
    WeComChannelConfig,
)
from long_invest.modules.notifications.contracts import DeliveryChannel
from long_invest.modules.notifications.templates import (
    RenderedTemplate,
    StrictTemplateRenderer,
    TemplateDefinition,
)

_WECOM_HOST = "qyapi.weixin.qq.com"
_WECOM_PATH = "/cgi-bin/webhook/send"
_MAX_MESSAGE_BYTES = 4096
_MAX_RESPONSE_BYTES = 64 * 1024
_EVENT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_TRANSIENT_BUSINESS_CODES = {-1, 45009}


class WeComRobotChannel:
    channel = DeliveryChannel.WECOM

    def __init__(
        self,
        *,
        config: WeComChannelConfig,
        webhook_url: str,
        client: httpx.AsyncClient,
        renderer: StrictTemplateRenderer | None = None,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
    ) -> None:
        self._config = config
        self._webhook_url = webhook_url
        self._client = client
        self._renderer = renderer or StrictTemplateRenderer()
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self._max_response_bytes = max_response_bytes
        self._validate_webhook_url(webhook_url)

    def validate_config(self, config: WeComChannelConfig) -> tuple[str, ...]:
        errors: list[str] = []
        if config.config_version <= 0:
            errors.append("config_version must be positive")
        if not config.target_fingerprint.strip():
            errors.append("target_fingerprint must not be empty")
        return tuple(errors)

    def render(
        self,
        template: TemplateDefinition,
        variables: dict[str, Any],
    ) -> RenderedTemplate:
        return self._renderer.render(template, variables)

    async def send(self, request: ChannelSendRequest) -> ChannelResult:
        return await self._send(request, test_message=False)

    async def test(self, request: ChannelSendRequest) -> ChannelResult:
        return await self._send(request, test_message=True)

    @staticmethod
    def _validate_webhook_url(webhook_url: str) -> None:
        parsed = urlsplit(webhook_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == _WECOM_HOST
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and parsed.path == _WECOM_PATH
            and not parsed.fragment
            and set(query) == {"key"}
            and len(query["key"]) == 1
            and bool(query["key"][0])
        )
        if not valid:
            raise ValueError("invalid enterprise WeCom robot webhook URL")

    async def _send(
        self,
        request: ChannelSendRequest,
        *,
        test_message: bool,
    ) -> ChannelResult:
        if not _EVENT_ID.fullmatch(request.event_id):
            return ChannelResult.permanent_failure(
                code="WECOM_INVALID_EVENT_ID",
                summary="notification event id is invalid",
            )

        content = request.text
        if test_message:
            content = f"[TEST MESSAGE] {content}"
        content = self._fit_message(content, request.event_id)
        payload = {"msgtype": "text", "text": {"content": content}}

        try:
            async with self._client.stream(
                "POST",
                self._webhook_url,
                json=payload,
                follow_redirects=False,
                headers={"Accept": "application/json"},
            ) as response:
                body = await self._read_limited(response)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            return ChannelResult.temporary_failure(
                code="WECOM_CONNECTION_FAILED",
                summary="enterprise WeCom could not be reached",
            )
        except httpx.WriteTimeout:
            return ChannelResult.outcome_unknown(
                code="WECOM_WRITE_TIMEOUT",
                summary="enterprise WeCom request completion is unknown",
            )
        except (httpx.ReadError, httpx.ReadTimeout, httpx.RemoteProtocolError):
            return ChannelResult.outcome_unknown(
                code="WECOM_RESPONSE_LOST",
                summary="enterprise WeCom response was not received",
            )
        except httpx.HTTPError:
            return ChannelResult.temporary_failure(
                code="WECOM_HTTP_FAILED",
                summary="enterprise WeCom request failed",
            )
        except _ResponseTooLarge:
            return ChannelResult.outcome_unknown(
                code="WECOM_RESPONSE_TOO_LARGE",
                summary="enterprise WeCom returned an oversized response",
            )

        if 300 <= response.status_code < 400:
            return ChannelResult.permanent_failure(
                code="WECOM_REDIRECT_REJECTED",
                summary="enterprise WeCom redirect was rejected",
                details={"http_status": response.status_code},
            )
        if response.status_code == 429 or response.status_code >= 500:
            return ChannelResult.temporary_failure(
                code="WECOM_HTTP_RETRYABLE",
                summary="enterprise WeCom is temporarily unavailable",
                details={"http_status": response.status_code},
            )
        if not 200 <= response.status_code < 300:
            return ChannelResult.permanent_failure(
                code="WECOM_HTTP_REJECTED",
                summary="enterprise WeCom rejected the request",
                details={"http_status": response.status_code},
            )

        result = self._decode_business_result(body)
        if result is None:
            return ChannelResult.outcome_unknown(
                code="WECOM_INVALID_RESPONSE",
                summary="enterprise WeCom returned an invalid response",
                details={"http_status": response.status_code},
            )
        if result == 0:
            return ChannelResult.success(
                summary="enterprise WeCom accepted the notification",
                details={"http_status": response.status_code, "errcode": result},
            )
        if result in _TRANSIENT_BUSINESS_CODES:
            return ChannelResult.temporary_failure(
                code="WECOM_BUSINESS_RETRYABLE",
                summary="enterprise WeCom temporarily rejected the notification",
                details={"http_status": response.status_code, "errcode": result},
            )
        return ChannelResult.permanent_failure(
            code="WECOM_BUSINESS_REJECTED",
            summary="enterprise WeCom rejected the notification",
            details={"http_status": response.status_code, "errcode": result},
        )

    async def _read_limited(self, response: httpx.Response) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self._max_response_bytes:
                    raise _ResponseTooLarge
            except ValueError:
                pass

        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > self._max_response_bytes:
                raise _ResponseTooLarge
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _decode_business_result(body: bytes) -> int | None:
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        errcode = value.get("errcode")
        if isinstance(errcode, int) and not isinstance(errcode, bool):
            return errcode
        return None

    @staticmethod
    def _fit_message(text: str, event_id: str) -> str:
        normalized = text.replace("\x00", "").strip()
        suffix = f"\n[event_id: {event_id}]"
        available = _MAX_MESSAGE_BYTES - len(suffix.encode("utf-8"))
        encoded = normalized.encode("utf-8")
        if len(encoded) > available:
            encoded = encoded[:available]
            while True:
                try:
                    normalized = encoded.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    encoded = encoded[:-1]
        return f"{normalized}{suffix}"


class _ResponseTooLarge(Exception):
    pass
