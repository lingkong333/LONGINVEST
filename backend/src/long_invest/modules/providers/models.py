from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from long_invest.platform.database.base import Base


class ProviderConfigVersion(Base):
    __tablename__ = "provider_config_version"
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProviderCapabilitySetting(Base):
    __tablename__ = "provider_capability_setting"
    __table_args__ = (
        UniqueConstraint("config_version", "provider_code", "capability"),
        CheckConstraint("priority >= 0", name="priority_nonnegative"),
        CheckConstraint("concurrency BETWEEN 1 AND 32", name="concurrency_range"),
        CheckConstraint("rate_per_second > 0", name="rate_positive"),
        CheckConstraint("timeout_seconds > 0", name="timeout_positive"),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_code: Mapped[str] = mapped_column(String(32), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    rate_per_second: Mapped[float] = mapped_column(Float, nullable=False)
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    auto_switch: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ProviderHealthState(Base):
    __tablename__ = "provider_health_state"
    __table_args__ = (UniqueConstraint("provider_code", "capability"),)
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    provider_code: Mapped[str] = mapped_column(String(32), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN")
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class ProviderCircuitHistory(Base):
    __tablename__ = "provider_circuit_history"
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    provider_code: Mapped[str] = mapped_column(String(32), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    from_state: Mapped[str] = mapped_column(String(32), nullable=False)
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ProviderFailureSample(Base):
    __tablename__ = "provider_failure_sample"
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    provider_code: Mapped[str] = mapped_column(String(32), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    error_code: Mapped[str] = mapped_column(String(100), nullable=False)
    sample: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
