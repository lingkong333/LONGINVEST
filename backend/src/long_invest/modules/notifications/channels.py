from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from long_invest.modules.notifications.contracts import (
    DeliveryChannel,
)
from long_invest.modules.notifications.contracts import (
    DeliveryOutcome as ChannelOutcome,
)
from long_invest.modules.notifications.security import (
    SecretReferenceValue,
    validate_notification_payload,
)
from long_invest.modules.notifications.templates import (
    RenderedTemplate,
    TemplateDefinition,
)

SecretReference = SecretReferenceValue


def _reject_newline(value: str) -> None:
    if "\r" in value or "\n" in value:
        raise ValueError("address and header values cannot contain a newline")


@dataclass(frozen=True, slots=True)
class WeComChannelConfig:
    config_version: int
    target_fingerprint: str
    webhook_secret_ref: SecretReference

    def __post_init__(self) -> None:
        if not isinstance(self.webhook_secret_ref, SecretReferenceValue):
            raise ValueError("webhook must be provided as a secret reference")

    def as_safe_dict(self) -> dict[str, Any]:
        return {
            "channel": DeliveryChannel.WECOM.value,
            "config_version": self.config_version,
            "target_fingerprint": self.target_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class EmailChannelConfig:
    config_version: int
    target_fingerprint: str
    password_secret_ref: SecretReference
    sender: str
    recipients: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.password_secret_ref, SecretReferenceValue):
            raise ValueError("password must be provided as a secret reference")
        _reject_newline(self.sender)
        if not 1 <= len(self.recipients) <= 5:
            raise ValueError("email channel requires between one and five recipients")
        for recipient in self.recipients:
            _reject_newline(recipient)

    def as_safe_dict(self) -> dict[str, Any]:
        return {
            "channel": DeliveryChannel.EMAIL.value,
            "config_version": self.config_version,
            "target_fingerprint": self.target_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class ChannelSendRequest:
    event_id: str
    deterministic_message_id: str
    subject: str | None
    text: str
    html: str | None = None


@dataclass(frozen=True, slots=True)
class ChannelResult:
    outcome: ChannelOutcome
    code: str
    summary: str
    retryable: bool
    possibly_delivered: bool
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        safe_details = validate_notification_payload(self.details)
        object.__setattr__(self, "details", safe_details)

    @classmethod
    def success(
        cls,
        *,
        summary: str,
        details: dict[str, Any] | None = None,
    ) -> "ChannelResult":
        return cls(
            ChannelOutcome.SUCCESS,
            "OK",
            summary,
            retryable=False,
            possibly_delivered=True,
            details=details or {},
        )

    @classmethod
    def temporary_failure(
        cls,
        *,
        code: str,
        summary: str,
        details: dict[str, Any] | None = None,
    ) -> "ChannelResult":
        return cls(
            ChannelOutcome.TEMPORARY_FAILURE,
            code,
            summary,
            retryable=True,
            possibly_delivered=False,
            details=details or {},
        )

    @classmethod
    def permanent_failure(
        cls,
        *,
        code: str,
        summary: str,
        details: dict[str, Any] | None = None,
    ) -> "ChannelResult":
        return cls(
            ChannelOutcome.PERMANENT_FAILURE,
            code,
            summary,
            retryable=False,
            possibly_delivered=False,
            details=details or {},
        )

    @classmethod
    def outcome_unknown(
        cls,
        *,
        code: str,
        summary: str,
        details: dict[str, Any] | None = None,
    ) -> "ChannelResult":
        return cls(
            ChannelOutcome.OUTCOME_UNKNOWN,
            code,
            summary,
            retryable=True,
            possibly_delivered=True,
            details=details or {},
        )

    def as_safe_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "code": self.code,
            "summary": self.summary,
            "retryable": self.retryable,
            "possibly_delivered": self.possibly_delivered,
            "details": self.details,
        }


ConfigT = TypeVar("ConfigT")


class NotificationChannel(Protocol[ConfigT]):
    channel: DeliveryChannel

    def validate_config(self, config: ConfigT) -> tuple[str, ...]: ...

    def render(
        self,
        template: TemplateDefinition,
        variables: dict[str, Any],
    ) -> RenderedTemplate: ...

    async def send(self, request: ChannelSendRequest) -> ChannelResult: ...

    async def test(self, request: ChannelSendRequest) -> ChannelResult: ...


class WeComRobotChannel(NotificationChannel[WeComChannelConfig], Protocol):
    channel: DeliveryChannel


class EmailChannel(NotificationChannel[EmailChannelConfig], Protocol):
    channel: DeliveryChannel
