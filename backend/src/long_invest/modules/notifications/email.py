import asyncio
import hashlib
import re
import smtplib
import ssl
from collections.abc import Callable, Iterable
from contextlib import suppress
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import parseaddr
from enum import StrEnum
from typing import Any

from long_invest.modules.notifications.channels import (
    ChannelResult,
    ChannelSendRequest,
    EmailChannelConfig,
)
from long_invest.modules.notifications.contracts import DeliveryChannel
from long_invest.modules.notifications.templates import (
    RenderedTemplate,
    StrictTemplateRenderer,
    TemplateDefinition,
)

SmtpFactory = Callable[..., smtplib.SMTP]

_MESSAGE_ID = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+")


class SmtpSecurity(StrEnum):
    SSL = "SSL"
    STARTTLS = "STARTTLS"


def _reject_newline(value: str, *, field: str) -> None:
    if "\r" in value or "\n" in value:
        raise ValueError(f"{field} cannot contain a newline")


def _mailbox(value: str, *, field: str) -> str:
    _reject_newline(value, field=field)
    display_name, address = parseaddr(value)
    if not address or parseaddr(address) != ("", address) or "@" not in address:
        raise ValueError(f"{field} must be a valid email address")
    if display_name and parseaddr(value)[1] != address:
        raise ValueError(f"{field} must be a valid email address")
    return address


def _message_id(value: str) -> str:
    _reject_newline(value, field="message ID")
    candidate = value.strip()
    if candidate.startswith("<") and candidate.endswith(">"):
        candidate = candidate[1:-1]
    if "@" not in candidate:
        candidate = (
            f"{hashlib.sha256(candidate.encode()).hexdigest()}@long-invest.local"
        )
    if not _MESSAGE_ID.fullmatch(candidate):
        raise ValueError("message ID contains unsupported characters")
    return f"<{candidate}>"


def _response_failure(
    *,
    stage: str,
    code: int,
    possibly_delivered: bool = False,
) -> ChannelResult:
    safe_stage = stage.upper()
    if possibly_delivered:
        return ChannelResult.outcome_unknown(
            code=f"EMAIL_{safe_stage}_OUTCOME_UNKNOWN",
            summary=f"SMTP {stage.lower()} result was not confirmed",
            details={"smtp_status": code},
        )
    if 400 <= code < 500:
        return ChannelResult.temporary_failure(
            code=f"EMAIL_{safe_stage}_TEMPORARY_FAILURE",
            summary=f"SMTP {stage.lower()} was temporarily rejected",
            details={"smtp_status": code},
        )
    return ChannelResult.permanent_failure(
        code=f"EMAIL_{safe_stage}_PERMANENT_FAILURE",
        summary=f"SMTP {stage.lower()} was rejected",
        details={"smtp_status": code},
    )


class SmtpEmailChannel:
    channel = DeliveryChannel.EMAIL

    def __init__(
        self,
        *,
        config: EmailChannelConfig,
        smtp_host: str,
        smtp_port: int,
        security: SmtpSecurity | str,
        allowed_hosts: Iterable[str],
        username: str | None = None,
        password: str | None = None,
        timeout_seconds: float = 10.0,
        smtp_factory: SmtpFactory | None = None,
        ssl_context_factory: Callable[[], ssl.SSLContext] = ssl.create_default_context,
        renderer: StrictTemplateRenderer | None = None,
    ) -> None:
        self._config = config
        self._smtp_host = smtp_host.strip().rstrip(".").lower()
        self._smtp_port = smtp_port
        self._security = SmtpSecurity(security)
        self._allowed_hosts = {
            host.strip().rstrip(".").lower() for host in allowed_hosts if host.strip()
        }
        self._username = username
        self._password = password
        self._timeout_seconds = timeout_seconds
        self._smtp_factory = smtp_factory
        self._ssl_context_factory = ssl_context_factory
        self._renderer = renderer or StrictTemplateRenderer()

        errors = self.validate_config(config)
        if errors:
            raise ValueError("; ".join(errors))

        self._sender = _mailbox(config.sender, field="sender")
        self._recipients = tuple(
            _mailbox(recipient, field="recipient") for recipient in config.recipients
        )

    def validate_config(self, config: EmailChannelConfig) -> tuple[str, ...]:
        errors: list[str] = []
        if self._smtp_host not in self._allowed_hosts:
            errors.append("SMTP host is not in the startup allowlist")
        if not 1 <= self._smtp_port <= 65535:
            errors.append("SMTP port must be between 1 and 65535")
        if self._timeout_seconds <= 0:
            errors.append("SMTP timeout must be positive")
        if self._username is not None:
            try:
                _reject_newline(self._username, field="SMTP username")
            except ValueError as exc:
                errors.append(str(exc))
            if not self._password:
                errors.append("SMTP password is required when username is configured")
        try:
            _mailbox(config.sender, field="sender")
            for recipient in config.recipients:
                _mailbox(recipient, field="recipient")
        except ValueError as exc:
            errors.append(str(exc))
        return tuple(errors)

    def render(
        self,
        template: TemplateDefinition,
        variables: dict[str, Any],
    ) -> RenderedTemplate:
        return self._renderer.render(template, variables)

    async def send(self, request: ChannelSendRequest) -> ChannelResult:
        return await asyncio.to_thread(self._send_sync, request)

    async def test(self, request: ChannelSendRequest) -> ChannelResult:
        return await self.send(request)

    def _build_message(self, request: ChannelSendRequest) -> EmailMessage:
        subject = request.subject or "LongInvest notification"
        _reject_newline(subject, field="subject")

        message = EmailMessage(policy=SMTP)
        message["From"] = self._config.sender
        message["To"] = ", ".join(self._config.recipients)
        message["Subject"] = subject
        message["Message-ID"] = _message_id(request.deterministic_message_id)
        message.set_content(request.text, charset="utf-8")
        if request.html is not None:
            message.add_alternative(request.html, subtype="html", charset="utf-8")
        return message

    def _connect(self, context: ssl.SSLContext) -> smtplib.SMTP:
        if self._security is SmtpSecurity.SSL:
            factory = self._smtp_factory or smtplib.SMTP_SSL
            return factory(
                host=self._smtp_host,
                port=self._smtp_port,
                timeout=self._timeout_seconds,
                context=context,
            )
        factory = self._smtp_factory or smtplib.SMTP
        client = factory(
            host=self._smtp_host,
            port=self._smtp_port,
            timeout=self._timeout_seconds,
        )
        client.ehlo()
        client.starttls(context=context)
        client.ehlo()
        return client

    def _send_sync(self, request: ChannelSendRequest) -> ChannelResult:
        try:
            message = self._build_message(request)
        except (TypeError, ValueError) as exc:
            return ChannelResult.permanent_failure(
                code="EMAIL_MESSAGE_INVALID",
                summary=str(exc),
            )

        try:
            context = self._ssl_context_factory()
            client = self._connect(context)
        except ssl.SSLCertVerificationError:
            return ChannelResult.permanent_failure(
                code="EMAIL_TLS_CERTIFICATE_INVALID",
                summary="SMTP TLS certificate verification failed",
            )
        except (TimeoutError, ConnectionError, OSError, smtplib.SMTPException) as exc:
            return ChannelResult.temporary_failure(
                code="EMAIL_CONNECT_FAILED",
                summary="SMTP connection failed",
                details={"error_type": type(exc).__name__},
            )

        try:
            result = self._deliver(client, message)
        finally:
            with suppress(OSError, smtplib.SMTPException):
                client.quit()
        return result

    def _deliver(self, client: smtplib.SMTP, message: EmailMessage) -> ChannelResult:
        if self._username is not None:
            try:
                client.login(self._username, self._password or "")
            except smtplib.SMTPAuthenticationError as exc:
                return ChannelResult.permanent_failure(
                    code="EMAIL_AUTH_FAILED",
                    summary="SMTP authentication was rejected",
                    details={"smtp_status": exc.smtp_code},
                )
            except smtplib.SMTPResponseException as exc:
                return _response_failure(stage="AUTH", code=exc.smtp_code)
            except (
                TimeoutError,
                ConnectionError,
                OSError,
                smtplib.SMTPException,
            ) as exc:
                return ChannelResult.temporary_failure(
                    code="EMAIL_AUTH_CONNECTION_FAILED",
                    summary="SMTP connection failed during authentication",
                    details={"error_type": type(exc).__name__},
                )

        stage = "MAIL"
        try:
            code, _ = client.mail(self._sender)
            if code != 250:
                return _response_failure(stage=stage, code=code)

            stage = "RCPT"
            for recipient in self._recipients:
                code, _ = client.rcpt(recipient)
                if code not in {250, 251}:
                    with suppress(OSError, smtplib.SMTPException):
                        client.rset()
                    return _response_failure(stage=stage, code=code)

            stage = "DATA"
            code, _ = client.data(message.as_bytes(policy=SMTP))
            if code != 250:
                return _response_failure(stage=stage, code=code)
        except smtplib.SMTPResponseException as exc:
            return _response_failure(stage=stage, code=exc.smtp_code)
        except (
            TimeoutError,
            ConnectionError,
            OSError,
            smtplib.SMTPServerDisconnected,
        ) as exc:
            if stage == "DATA":
                return ChannelResult.outcome_unknown(
                    code="EMAIL_DATA_OUTCOME_UNKNOWN",
                    summary="SMTP DATA result was not confirmed",
                    details={"error_type": type(exc).__name__},
                )
            return ChannelResult.temporary_failure(
                code=f"EMAIL_{stage}_CONNECTION_FAILED",
                summary=f"SMTP connection failed during {stage.lower()}",
                details={"error_type": type(exc).__name__},
            )
        except smtplib.SMTPException as exc:
            return ChannelResult.temporary_failure(
                code=f"EMAIL_{stage}_FAILED",
                summary=f"SMTP {stage.lower()} failed",
                details={"error_type": type(exc).__name__},
            )

        return ChannelResult.success(
            summary="email accepted by SMTP server",
            details={"recipient_count": len(self._recipients)},
        )
