from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NotificationPolicyValue(StrictModel):
    enabled: bool = True
    channels: list[Literal["WECOM", "EMAIL"]] = Field(default_factory=list)


class SystemAlertPolicyValue(StrictModel):
    enabled: bool = True
    warning: list[Literal["WECOM", "EMAIL"]] = Field(default_factory=list)
    error: list[Literal["WECOM", "EMAIL"]] = Field(default_factory=list)
    critical: list[Literal["WECOM", "EMAIL"]] = Field(default_factory=list)
    recovered: list[Literal["WECOM", "EMAIL"]] = Field(default_factory=list)
    daily_unresolved: list[Literal["WECOM", "EMAIL"]] = Field(
        default_factory=list
    )


class WeComChannelValue(StrictModel):
    enabled: bool = False
    timeout_seconds: float = Field(default=5.0, ge=1, le=15)


class EmailChannelValue(StrictModel):
    enabled: bool = False
    smtp_host: str = Field(default="", max_length=253)
    smtp_port: int = Field(default=465, ge=1, le=65535)
    security: Literal["SSL", "STARTTLS"] = "SSL"
    username: str = Field(default="", max_length=320)
    sender: str = Field(default="", max_length=320)
    recipients: list[str] = Field(default_factory=list, max_length=5)
    timeout_seconds: float = Field(default=10.0, ge=1, le=30)


SETTING_SCHEMAS: dict[str, tuple[type[BaseModel], str]] = {
    "notification.policy.global": (NotificationPolicyValue, "全局通知开关和默认渠道"),
    "notification.policy.signals": (NotificationPolicyValue, "信号通知开关和渠道"),
    "notification.policy.system_alerts": (
        SystemAlertPolicyValue,
        "系统告警通知开关和渠道",
    ),
    "notification.channel.wecom": (WeComChannelValue, "企业微信机器人运行参数"),
    "notification.channel.email": (EmailChannelValue, "邮件服务器和固定收件人"),
}

SECRET_KEYS = frozenset({"notification.wecom.webhook", "notification.email.password"})


def validate_setting(key: str, value: Any) -> dict[str, Any]:
    definition = SETTING_SCHEMAS.get(key)
    if definition is None:
        raise KeyError(key)
    model, _description = definition
    return TypeAdapter(model).validate_python(value).model_dump(mode="json")
