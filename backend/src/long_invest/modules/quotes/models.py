from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from long_invest.platform.database.base import Base


class QuoteCycle(Base):
    __tablename__ = "quote_cycle"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_scope", "idempotency_key", name="uq_quote_cycle_idempotency"
        ),
        UniqueConstraint(
            "schedule_occurrence_id", name="uq_quote_cycle_schedule_occurrence"
        ),
        CheckConstraint("deadline_at > started_at", name="deadline"),
        CheckConstraint("expected_count > 0", name="expected_positive"),
        CheckConstraint("timeout_seconds BETWEEN 10 AND 60", name="timeout_supported"),
        CheckConstraint(
            "subscription_snapshot_version IS NULL OR "
            "subscription_snapshot_version > 0",
            name="subscription_snapshot_positive",
        ),
        CheckConstraint(
            "valid_count >= 0 AND missing_count >= 0 AND conflict_count >= 0 "
            "AND failed_count >= 0",
            name="counts_nonnegative",
        ),
        CheckConstraint(
            "status IN ('PENDING','FETCHING','FINALIZING','READY','PARTIAL',"
            "'FAILED','MISSED','CANCELED')",
            name="status_valid",
        ),
        Index("ix_quote_cycle_status_deadline", "status", "deadline_at"),
    )

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("id", uuid4())
        kwargs.setdefault("valid_count", 0)
        kwargs.setdefault("missing_count", 0)
        kwargs.setdefault("conflict_count", 0)
        kwargs.setdefault("failed_count", 0)
        super().__init__(**kwargs)

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    schedule_occurrence_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    universe_snapshot_id: Mapped[str] = mapped_column(String(200), nullable=False)
    universe_snapshot_version: Mapped[int] = mapped_column(Integer, nullable=False)
    subscription_snapshot_version: Mapped[int | None] = mapped_column(Integer)
    idempotency_scope: Mapped[str] = mapped_column(String(200), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    expected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conflict_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_reason: Mapped[str | None] = mapped_column(String(500))
    items: Mapped[list["QuoteCycleItem"]] = relationship(
        back_populates="cycle",
        cascade="all, delete-orphan",
        order_by="QuoteCycleItem.symbol",
    )


class QuoteCycleItem(Base):
    __tablename__ = "quote_cycle_item"
    __table_args__ = (
        UniqueConstraint("cycle_id", "symbol", name="uq_quote_cycle_item_symbol"),
        CheckConstraint("volume IS NULL OR volume >= 0", name="volume_nonnegative"),
        CheckConstraint("amount IS NULL OR amount >= 0", name="amount_nonnegative"),
        CheckConstraint(
            "expected_subscription_version IS NULL OR "
            "expected_subscription_version > 0",
            name="expected_subscription_positive",
        ),
        CheckConstraint(
            "status IN ('VALID','MISSING','STALE','CONFLICT','INVALID','TIMEOUT',"
            "'PROVIDER_FAILED','NOT_EXPECTED_TO_TRADE')",
            name="status_valid",
        ),
        Index("ix_quote_cycle_item_cycle_status", "cycle_id", "status"),
    )

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("id", uuid4())
        kwargs.setdefault("eligible_for_evaluation", False)
        super().__init__(**kwargs)

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    cycle_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("quote_cycle.id", ondelete="CASCADE"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    expected_subscription_version: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    open: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    high: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    low: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    previous_close: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    volume: Mapped[int | None] = mapped_column(Integer)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    quote_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(80))
    conflict_evidence: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    eligible_for_evaluation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    cycle: Mapped[QuoteCycle] = relationship(back_populates="items")
