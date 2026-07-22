import asyncio
import json

import httpx
import pytest

from long_invest.modules.notifications.channels import (
    ChannelOutcome,
    ChannelSendRequest,
    SecretReference,
    WeComChannelConfig,
)
from long_invest.modules.notifications.templates import TemplateDefinition
from long_invest.modules.notifications.wecom import WeComRobotChannel

WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fixed-target"


def config() -> WeComChannelConfig:
    return WeComChannelConfig(
        config_version=1,
        target_fingerprint="wecom:1234",
        webhook_secret_ref=SecretReference("secret://notifications/wecom/webhook"),
    )


def request() -> ChannelSendRequest:
    return ChannelSendRequest(
        event_id="event-123",
        deterministic_message_id="message-123",
        subject=None,
        text="浦发银行到达目标价",
    )


def run_send(
    handler,
    *,
    webhook_url: str = WEBHOOK,
    max_response_bytes: int = 64 * 1024,
    test_message: bool = False,
):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            channel = WeComRobotChannel(
                config=config(),
                webhook_url=webhook_url,
                client=client,
                max_response_bytes=max_response_bytes,
            )
            operation = channel.test if test_message else channel.send
            return await operation(request())

    return asyncio.run(scenario())


def test_success_checks_business_result_and_includes_event_id() -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        assert http_request.url == WEBHOOK
        payload = json.loads(http_request.content)
        assert payload == {
            "msgtype": "text",
            "text": {"content": "浦发银行到达目标价\n[event_id: event-123]"},
        }
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    result = run_send(handler)

    assert result.outcome is ChannelOutcome.SUCCESS
    assert result.details == {"http_status": 200, "errcode": 0}


@pytest.mark.parametrize(
    "url",
    [
        "http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x",
        "https://evil.invalid/cgi-bin/webhook/send?key=x",
        "https://qyapi.weixin.qq.com.evil.invalid/cgi-bin/webhook/send?key=x",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x&url=https://evil.invalid",
        "https://qyapi.weixin.qq.com/other?key=x",
    ],
)
def test_constructor_rejects_non_allowlisted_webhook_targets(url: str) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError, match="webhook URL"):
                WeComRobotChannel(config=config(), webhook_url=url, client=client)

    asyncio.run(scenario())


def test_redirect_is_not_followed_and_is_a_permanent_failure() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "https://evil.invalid"})

    result = run_send(handler)

    assert calls == 1
    assert result.code == "WECOM_REDIRECT_REJECTED"
    assert result.outcome is ChannelOutcome.PERMANENT_FAILURE


def test_http_and_business_failures_are_classified_separately() -> None:
    temporary = run_send(lambda _: httpx.Response(503, text="busy"))
    rejected = run_send(
        lambda _: httpx.Response(200, json={"errcode": 93000, "errmsg": "invalid"})
    )
    busy = run_send(
        lambda _: httpx.Response(200, json={"errcode": -1, "errmsg": "busy"})
    )

    assert temporary.outcome is ChannelOutcome.TEMPORARY_FAILURE
    assert rejected.outcome is ChannelOutcome.PERMANENT_FAILURE
    assert rejected.details["errcode"] == 93000
    assert busy.outcome is ChannelOutcome.TEMPORARY_FAILURE


def test_invalid_or_oversized_success_response_is_outcome_unknown() -> None:
    invalid = run_send(lambda _: httpx.Response(200, content=b"not-json"))
    oversized = run_send(
        lambda _: httpx.Response(200, content=b"x" * 33),
        max_response_bytes=32,
    )

    assert invalid.outcome is ChannelOutcome.OUTCOME_UNKNOWN
    assert invalid.possibly_delivered is True
    assert oversized.code == "WECOM_RESPONSE_TOO_LARGE"
    assert oversized.outcome is ChannelOutcome.OUTCOME_UNKNOWN


def test_read_timeout_after_send_is_outcome_unknown() -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("response lost", request=http_request)

    result = run_send(handler)

    assert result.code == "WECOM_RESPONSE_LOST"
    assert result.outcome is ChannelOutcome.OUTCOME_UNKNOWN


def test_test_message_is_marked_and_template_rendering_stays_strict() -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        content = json.loads(http_request.content)["text"]["content"]
        assert content.startswith("[TEST MESSAGE]")
        return httpx.Response(200, json={"errcode": 0})

    result = run_send(handler, test_message=True)

    async def render() -> str:
        async with httpx.AsyncClient() as client:
            channel = WeComRobotChannel(
                config=config(), webhook_url=WEBHOOK, client=client
            )
            rendered = channel.render(
                TemplateDefinition("signal", "v1", "{{ name }} 到达目标价"),
                {"name": "浦发银行"},
            )
            return rendered.text

    assert result.outcome is ChannelOutcome.SUCCESS
    assert asyncio.run(render()) == "浦发银行 到达目标价"


def test_long_multibyte_message_is_truncated_without_losing_event_id() -> None:
    observed: dict[str, str] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        observed.update(json.loads(http_request.content)["text"])
        return httpx.Response(200, json={"errcode": 0})

    original = request()
    long_request = ChannelSendRequest(
        event_id=original.event_id,
        deterministic_message_id=original.deterministic_message_id,
        subject=None,
        text="股" * 5000,
    )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            channel = WeComRobotChannel(
                config=config(), webhook_url=WEBHOOK, client=client
            )
            return await channel.send(long_request)

    result = asyncio.run(scenario())

    assert result.outcome is ChannelOutcome.SUCCESS
    assert len(observed["content"].encode("utf-8")) <= 4096
    assert observed["content"].endswith("[event_id: event-123]")
