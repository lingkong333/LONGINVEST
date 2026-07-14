import importlib
import importlib.util
import json

import pytest


def load_module(name: str):
    module_name = f"long_invest.modules.notifications.{name}"
    assert importlib.util.find_spec(module_name) is not None, (
        f"notification {name} capability is not implemented"
    )
    return importlib.import_module(module_name)


def test_channel_config_accepts_only_a_secret_reference_and_serializes_safely() -> None:
    channels = load_module("channels")

    config = channels.WeComChannelConfig(
        config_version=3,
        target_fingerprint="wecom:7ad4",
        webhook_secret_ref=channels.SecretReference(
            "secret://notifications/wecom/webhook"
        ),
    )

    serialized = json.dumps(config.as_safe_dict())
    assert config.webhook_secret_ref.key == "secret://notifications/wecom/webhook"
    assert "secret://" not in serialized
    assert "webhook" not in serialized.lower()
    assert config.as_safe_dict() == {
        "channel": "WECOM",
        "config_version": 3,
        "target_fingerprint": "wecom:7ad4",
    }

    with pytest.raises(ValueError, match="secret reference"):
        channels.SecretReference("https://example.invalid/hook?key=plaintext")


def test_email_config_rejects_header_injection() -> None:
    channels = load_module("channels")

    with pytest.raises(ValueError, match="newline"):
        channels.EmailChannelConfig(
            config_version=1,
            target_fingerprint="mail:1",
            password_secret_ref=channels.SecretReference(
                "secret://notifications/email/password"
            ),
            sender="sender@example.com\nBcc: stolen@example.com",
            recipients=("owner@example.com",),
        )


def test_channel_results_distinguish_temporary_permanent_and_unknown() -> None:
    channels = load_module("channels")

    temporary = channels.ChannelResult.temporary_failure(
        code="CHANNEL_TIMEOUT", summary="upstream timed out"
    )
    permanent = channels.ChannelResult.permanent_failure(
        code="CHANNEL_AUTH_FAILED", summary="credentials rejected"
    )
    unknown = channels.ChannelResult.outcome_unknown(
        code="CHANNEL_RESPONSE_LOST", summary="response was not received"
    )

    assert temporary.outcome is channels.ChannelOutcome.TEMPORARY_FAILURE
    assert temporary.retryable is True
    assert temporary.possibly_delivered is False
    assert permanent.outcome is channels.ChannelOutcome.PERMANENT_FAILURE
    assert permanent.retryable is False
    assert unknown.outcome is channels.ChannelOutcome.OUTCOME_UNKNOWN
    assert unknown.retryable is True
    assert unknown.possibly_delivered is True


def test_channel_result_can_report_an_explicit_success() -> None:
    channels = load_module("channels")

    result = channels.ChannelResult.success(summary="accepted")

    assert result.outcome is channels.ChannelOutcome.SUCCESS
    assert result.retryable is False
    assert result.possibly_delivered is True


def test_wecom_and_email_protocols_expose_the_same_four_operations() -> None:
    channels = load_module("channels")

    expected = {"validate_config", "render", "send", "test"}
    assert expected <= set(dir(channels.WeComRobotChannel))
    assert expected <= set(dir(channels.EmailChannel))


def test_channel_result_rejects_unsafe_diagnostic_payloads() -> None:
    channels = load_module("channels")

    with pytest.raises(ValueError, match="sensitive"):
        channels.ChannelResult.temporary_failure(
            code="CHANNEL_TIMEOUT",
            summary="timeout",
            details={"smtp_password": "plaintext"},
        )


def test_strict_template_renderer_reports_missing_fields() -> None:
    templates = load_module("templates")
    definition = templates.TemplateDefinition(
        template_type="signal.high",
        version="v1",
        text="{{ symbol }} reached {{ price }} at {{ quote_time }}",
        subject="Signal for {{ symbol }}",
    )

    with pytest.raises(templates.TemplateRenderError) as exc_info:
        templates.StrictTemplateRenderer().render(
            definition,
            {"symbol": "600000.SH", "price": "12.34"},
        )

    assert exc_info.value.code == "NOTIFICATION_TEMPLATE_MISSING_FIELD"
    assert exc_info.value.missing_fields == ("quote_time",)


def test_template_renderer_escapes_html_and_marks_test_messages() -> None:
    templates = load_module("templates")
    definition = templates.TemplateDefinition(
        template_type="notification.test",
        version="v2",
        text="Stock {{ name }}",
        html="<p>Stock {{ name }}</p>",
        subject="Channel check",
    )

    rendered = templates.StrictTemplateRenderer().render(
        definition,
        {"name": "<script>alert(1)</script>"},
        test_message=True,
    )

    assert rendered.subject.startswith("[TEST]")
    assert rendered.text.startswith("[TEST MESSAGE]")
    assert "<script>" not in rendered.html
    assert "&lt;script&gt;" in rendered.html


def test_template_renderer_rejects_email_subject_newline_injection() -> None:
    templates = load_module("templates")
    definition = templates.TemplateDefinition(
        template_type="signal.high",
        version="v1",
        text="Stock {{ name }}",
        subject="Signal for {{ name }}",
    )

    with pytest.raises(templates.TemplateRenderError) as exc_info:
        templates.StrictTemplateRenderer().render(
            definition,
            {"name": "600000.SH\nBcc: stolen@example.com"},
        )

    assert exc_info.value.code == "NOTIFICATION_TEMPLATE_UNSAFE"
