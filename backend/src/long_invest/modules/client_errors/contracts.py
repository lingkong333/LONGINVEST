from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ClientErrorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    route: str = Field(min_length=1, max_length=300)
    frontend_version: str = Field(min_length=1, max_length=64)
    error_type: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=4096)
    browser_summary: str = Field(min_length=1, max_length=500)
    request_id: str | None = Field(default=None, max_length=128)
    occurred_at: datetime

    @field_validator("route")
    @classmethod
    def validate_route(cls, value: str) -> str:
        if not value.startswith("/") or "?" in value or "#" in value:
            raise ValueError("route must be an absolute page path without query data")
        return value


class ClientErrorReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fingerprint: str
    sampled: bool
