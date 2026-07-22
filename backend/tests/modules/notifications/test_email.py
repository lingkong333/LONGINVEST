import asyncio
import smtplib
import ssl
from email import message_from_bytes
from email.message import Message
from typing import Any

import pytest

from long_invest.modules.notifications.channels import (
    ChannelOutcome,
    ChannelSendRequest,
    EmailChannelConfig,
    SecretReference,
)
from long_invest.modules.notifications.email import SmtpEmailChannel, SmtpSecurity
from long_invest.modules.notifications.templates import TemplateDefinition


class FakeSmtp:
    def __init__(
        self,
        *,
        mail_response: tuple[int, bytes] = (250, b"ok"),
        rcpt_response: tuple[int, bytes] = (250, b"ok"),
        data_response: tuple[int, bytes] = (250, b"ok"),
        login_error: Exception | None = None,
        data_error: Exception | None = None,
    ) -> None:
        self.mail_response = mail_response
        self.rcpt_response = rcpt_response
        self.data_response = data_response
        self.login_error = login_error
        self.data_error = data_error
        self.starttls_context: ssl.SSLContext | None = None
        self.login_args: tuple[str, str] | None = None
        self.envelope_sender: str | None = None
        self.envelope_recipients: list[str] = []
        self.message: Message | None = None
        self.quit_called = False

    def ehlo(self) -> tuple[int, bytes]:
        return 250, b"ok"

    def starttls(self, *, context: ssl.SSLContext) -> tuple[int, bytes]:
        self.starttls_context = context
        return 220, b"ready"

    def login(self, username: str, password: str) -> tuple[int, bytes]:
        if self.login_error is not None:
            raise self.login_error
        self.login_args = (username, password)
        return 235, b"ok"

    def mail(self, sender: str) -> tuple[int, bytes]:
        self.envelope_sender = sender
        return self.mail_response

    def rcpt(self, recipient: str) -> tuple[int, bytes]:
        self.envelope_recipients.append(recipient)
        return self.rcpt_response

    def data(self, content: bytes) -> tuple[int, bytes]:
        if self.data_error is not None:
            raise self.data_error
        self.message = message_from_bytes(content)
        return self.data_response

    def rset(self) -> tuple[int, bytes]:
        return 250, b"ok"

    def quit(self) -> tuple[int, bytes]:
        self.quit_called = True
        return 221, b"bye"


def make_config(
    *, recipients: tuple[str, ...] = ("owner@example.com",)
) -> EmailChannelConfig:
    return EmailChannelConfig(
        config_version=2,
        target_fingerprint="email:7ad4",
        password_secret_ref=SecretReference("secret://notifications/email/password"),
        sender="LongInvest <alerts@example.com>",
        recipients=recipients,
    )


def make_request(**changes: Any) -> ChannelSendRequest:
    values: dict[str, Any] = {
        "event_id": "evt-123",
        "deterministic_message_id": "delivery-123@example.com",
        "subject": "Price alert",
        "text": "Plain text",
        "html": "<p>HTML text</p>",
    }
    values.update(changes)
    return ChannelSendRequest(**values)


def make_channel(
    smtp: FakeSmtp,
    *,
    security: SmtpSecurity = SmtpSecurity.STARTTLS,
    allowed_hosts: tuple[str, ...] = ("smtp.example.com",),
    config: EmailChannelConfig | None = None,
) -> SmtpEmailChannel:
    return SmtpEmailChannel(
        config=config or make_config(),
        smtp_host="smtp.example.com",
        smtp_port=587,
        security=security,
        allowed_hosts=allowed_hosts,
        username="mailer",
        password="not-serialized",
        smtp_factory=lambda **_: smtp,
    )


def test_starttls_send_uses_verified_context_and_multipart_utf8_message() -> None:
    smtp = FakeSmtp()
    channel = make_channel(smtp)

    result = asyncio.run(channel.send(make_request()))

    assert result.outcome is ChannelOutcome.SUCCESS
    assert result.details == {"recipient_count": 1}
    assert isinstance(smtp.starttls_context, ssl.SSLContext)
    assert smtp.starttls_context.verify_mode is ssl.CERT_REQUIRED
    assert smtp.starttls_context.check_hostname is True
    assert smtp.login_args == ("mailer", "not-serialized")
    assert smtp.envelope_sender == "alerts@example.com"
    assert smtp.envelope_recipients == ["owner@example.com"]
    assert smtp.message is not None
    assert smtp.message["Message-ID"] == "<delivery-123@example.com>"
    assert smtp.message.is_multipart()
    assert smtp.quit_called is True


def test_internal_delivery_id_becomes_a_stable_valid_message_id() -> None:
    channel = make_channel(FakeSmtp())
    request = make_request(deterministic_message_id="notification:123:EMAIL:1")

    first = channel._build_message(request)["Message-ID"]
    second = channel._build_message(request)["Message-ID"]

    assert first == second
    assert first.endswith("@long-invest.local>")


def test_ssl_mode_passes_verified_context_to_ssl_factory() -> None:
    smtp = FakeSmtp()
    captured: dict[str, Any] = {}

    def factory(**kwargs: Any) -> FakeSmtp:
        captured.update(kwargs)
        return smtp

    channel = SmtpEmailChannel(
        config=make_config(),
        smtp_host="smtp.example.com",
        smtp_port=465,
        security=SmtpSecurity.SSL,
        allowed_hosts=("smtp.example.com",),
        username="mailer",
        password="password",
        smtp_factory=factory,
    )

    result = asyncio.run(channel.send(make_request()))

    assert result.outcome is ChannelOutcome.SUCCESS
    assert captured["host"] == "smtp.example.com"
    assert captured["port"] == 465
    assert captured["context"].verify_mode is ssl.CERT_REQUIRED


def test_disallowed_smtp_host_is_rejected_before_any_connection() -> None:
    with pytest.raises(ValueError, match="allowlist"):
        make_channel(FakeSmtp(), allowed_hosts=("mail.example.com",))


def test_subject_and_message_id_header_injection_are_permanent_failures() -> None:
    channel = make_channel(FakeSmtp())

    subject = asyncio.run(channel.send(make_request(subject="Alert\nBcc: bad@example")))
    message_id = asyncio.run(
        channel.send(make_request(deterministic_message_id="id\r\nX-Evil: yes"))
    )

    assert subject.outcome is ChannelOutcome.PERMANENT_FAILURE
    assert subject.code == "EMAIL_MESSAGE_INVALID"
    assert message_id.outcome is ChannelOutcome.PERMANENT_FAILURE
    assert message_id.code == "EMAIL_MESSAGE_INVALID"


def test_authentication_rejection_is_permanent_and_does_not_expose_password() -> None:
    smtp = FakeSmtp(
        login_error=smtplib.SMTPAuthenticationError(535, b"secret response")
    )

    result = asyncio.run(make_channel(smtp).send(make_request()))

    assert result.outcome is ChannelOutcome.PERMANENT_FAILURE
    assert result.code == "EMAIL_AUTH_FAILED"
    assert result.details == {"smtp_status": 535}
    assert "secret" not in str(result.as_safe_dict()).lower()


@pytest.mark.parametrize("stage", ["MAIL", "RCPT", "DATA"])
def test_temporary_smtp_response_is_classified_by_stage(stage: str) -> None:
    kwargs = {f"{stage.lower()}_response": (451, b"try later")}
    smtp = FakeSmtp(**kwargs)

    result = asyncio.run(make_channel(smtp).send(make_request()))

    assert result.outcome is ChannelOutcome.TEMPORARY_FAILURE
    assert result.code == f"EMAIL_{stage}_TEMPORARY_FAILURE"
    assert result.retryable is True


def test_permanent_recipient_rejection_does_not_submit_message_data() -> None:
    smtp = FakeSmtp(rcpt_response=(550, b"unknown user"))

    result = asyncio.run(make_channel(smtp).send(make_request()))

    assert result.outcome is ChannelOutcome.PERMANENT_FAILURE
    assert result.code == "EMAIL_RCPT_PERMANENT_FAILURE"
    assert smtp.message is None


def test_disconnect_while_waiting_for_data_result_is_outcome_unknown() -> None:
    smtp = FakeSmtp(data_error=smtplib.SMTPServerDisconnected("lost response"))

    result = asyncio.run(make_channel(smtp).send(make_request()))

    assert result.outcome is ChannelOutcome.OUTCOME_UNKNOWN
    assert result.code == "EMAIL_DATA_OUTCOME_UNKNOWN"
    assert result.possibly_delivered is True


def test_render_uses_strict_renderer_and_test_delegates_to_send() -> None:
    smtp = FakeSmtp()
    channel = make_channel(smtp)
    rendered = channel.render(
        TemplateDefinition(
            template_type="notification.test",
            version="v1",
            subject="Test {{ symbol }}",
            text="Price {{ price }}",
            html="<p>{{ symbol }}</p>",
        ),
        {"symbol": "<600000.SH>", "price": "12.34"},
    )

    result = asyncio.run(
        channel.test(
            make_request(
                subject=rendered.subject,
                text=rendered.text,
                html=rendered.html,
            )
        )
    )

    assert result.outcome is ChannelOutcome.SUCCESS
    assert rendered.html == "<p>&lt;600000.SH&gt;</p>"
