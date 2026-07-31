from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from long_invest.platform.database.base import Base


class ProviderConfigVersion(Base):
    __tablename__ = "provider_config_version"
    __table_args__ = (
        UniqueConstraint("provider_code", "version"),
        CheckConstraint(
            "provider_code IN ('EASTMONEY', 'SINA', 'TUSHARE', 'BAOSTOCK', 'TENCENT')",
            name="provider_code_supported",
        ),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_code: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProviderCapabilitySetting(Base):
    __tablename__ = "provider_capability_setting"
    __table_args__ = (
        UniqueConstraint("config_version", "provider_code", "capability"),
        CheckConstraint(
            "provider_code IN ('EASTMONEY', 'SINA', 'TUSHARE', 'BAOSTOCK', 'TENCENT')",
            name="provider_code_supported",
        ),
        CheckConstraint("priority >= 0", name="priority_nonnegative"),
        CheckConstraint("concurrency >= 1", name="concurrency_positive"),
        CheckConstraint("daily_limit >= 1", name="daily_limit_positive"),
        CheckConstraint("min_interval_seconds >= 0", name="min_interval_nonnegative"),
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
    daily_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=50_000)
    min_interval_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.5
    )


class ProviderCapabilityRegistration(Base):
    __tablename__ = "provider_capability_registration"
    __table_args__ = (
        UniqueConstraint("provider_code", "capability"),
        CheckConstraint(
            "probe_status IN ('UNKNOWN','PASSED','FAILED')",
            name="probe_status_valid",
        ),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    provider_code: Mapped[str] = mapped_column(String(32), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    adapter_code: Mapped[str] = mapped_column(String(32), nullable=False)
    interface_code: Mapped[str] = mapped_column(String(500), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_ref: Mapped[str | None] = mapped_column(String(255))
    probe_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="UNKNOWN"
    )
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderRoutePolicyVersion(Base):
    __tablename__ = "provider_route_policy_version"
    __table_args__ = (UniqueConstraint("capability", "version"),)
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    fixed_provider_code: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProviderBudgetPolicy(Base):
    __tablename__ = "provider_budget_policy"
    __table_args__ = (
        UniqueConstraint("config_version", "provider_code"),
        CheckConstraint("daily_limit >= 1", name="daily_limit_positive"),
        CheckConstraint("max_concurrency >= 1", name="max_concurrency_positive"),
        CheckConstraint("realtime_reserved >= 0", name="realtime_reserved_nonnegative"),
        CheckConstraint("daily_reserved >= 0", name="daily_reserved_nonnegative"),
        CheckConstraint(
            "realtime_reserved + daily_reserved < daily_limit",
            name="reserved_below_limit",
        ),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_code: Mapped[str] = mapped_column(String(32), nullable=False)
    daily_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=50_000)
    reset_timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Asia/Shanghai"
    )
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    realtime_reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
    daily_reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=500)


class ProviderBudgetUsage(Base):
    __tablename__ = "provider_budget_usage"
    __table_args__ = (
        UniqueConstraint("provider_code", "capability", "budget_date"),
        CheckConstraint("used_count >= 0", name="used_count_nonnegative"),
        Index("ix_provider_budget_usage_provider_date", "provider_code", "budget_date"),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    provider_code: Mapped[str] = mapped_column(String(32), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    budget_date: Mapped[date] = mapped_column(Date, nullable=False)
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_request_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latest_limit_reason: Mapped[str | None] = mapped_column(String(100))
    latest_limited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderRequestLease(Base):
    __tablename__ = "provider_request_lease"
    __table_args__ = (
        UniqueConstraint("token"),
        Index(
            "ix_provider_request_lease_active",
            "provider_code",
            "capability",
            "expires_at",
            postgresql_where=text("released_at IS NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    token: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_code: Mapped[str] = mapped_column(String(32), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderHealthState(Base):
    __tablename__ = "provider_health_state"
    __table_args__ = (
        UniqueConstraint("provider_code", "capability"),
        CheckConstraint(
            "provider_code IN ('EASTMONEY', 'SINA', 'TUSHARE', 'BAOSTOCK', 'TENCENT')",
            name="provider_code_supported",
        ),
    )
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
    __table_args__ = (
        CheckConstraint(
            "provider_code IN ('EASTMONEY', 'SINA', 'TUSHARE', 'BAOSTOCK', 'TENCENT')",
            name="provider_code_supported",
        ),
    )
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


class ProviderCircuitState(Base):
    __tablename__ = "provider_circuit_state"
    __table_args__ = (
        UniqueConstraint("provider_code", "capability"),
        CheckConstraint(
            "provider_code IN ('EASTMONEY', 'SINA', 'TUSHARE', 'BAOSTOCK', 'TENCENT')",
            name="provider_code_supported",
        ),
        CheckConstraint("consecutive_failures >= 0", name="failures_nonnegative"),
        CheckConstraint("cooldown_index BETWEEN 0 AND 2", name="cooldown_index_range"),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    provider_code: Mapped[str] = mapped_column(String(32), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="CLOSED")
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    cooldown_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ProviderMutationRequest(Base):
    __tablename__ = "provider_mutation_request"
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(200), nullable=False, unique=True
    )
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[str] = mapped_column(String(128), nullable=False)
    response_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trusted_ip: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProviderFailureSample(Base):
    __tablename__ = "provider_failure_sample"
    __table_args__ = (
        CheckConstraint(
            "provider_code IN ('EASTMONEY', 'SINA', 'TUSHARE', 'BAOSTOCK', 'TENCENT')",
            name="provider_code_supported",
        ),
    )
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
