from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WatchlistBatchStatus(StrEnum):
    CREATED = "CREATED"
    REUSED = "REUSED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class WatchlistMutation(StrictContract):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    display_order: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=200)
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("name", "description", "reason", "idempotency_key", mode="before")
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class WatchlistBatchInput(StrictContract):
    symbols: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def copy_symbols(cls, value: Any) -> Any:
        if isinstance(value, dict) and "symbols" in value:
            symbols = value["symbols"]
            if isinstance(symbols, (list, tuple)):
                return {**value, "symbols": tuple(symbols)}
        return value


class WatchlistItemView(StrictContract):
    id: UUID
    watchlist_id: UUID
    security_id: UUID
    symbol: str
    source: str | None = None


class WatchlistView(StrictContract):
    id: UUID
    owner_user_id: UUID
    name: str
    description: str | None
    display_order: int
    version: int = Field(ge=1)
    archived: bool
    items: tuple[WatchlistItemView, ...] = ()


class WatchlistBatchItem(StrictContract):
    symbol: str
    status: WatchlistBatchStatus
    item: WatchlistItemView | None = None
    error_code: str | None = None


class WatchlistItemMutationResult(StrictContract):
    item: WatchlistItemView
    version: int = Field(ge=1)
    created: bool


class WatchlistItemRemovalResult(StrictContract):
    removed: bool
    pause_recommended: bool
    version: int = Field(ge=1)
