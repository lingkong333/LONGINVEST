from datetime import date, datetime, time
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Time,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from long_invest.platform.database.base import Base


class ImmutableCalendarFact:
    @staticmethod
    def _reject_mutation(_mapper: Any, _connection: Any, _target: Any) -> None:
        raise TypeError("calendar facts are immutable")


class TradingCalendarVersion(ImmutableCalendarFact, Base):
    __tablename__ = "trading_calendar_version"
    __table_args__ = (
        UniqueConstraint(
            "market", "version_number", name="uq_calendar_version_number"
        ),
        UniqueConstraint(
            "market",
            "source",
            "source_version",
            name="uq_calendar_source_version",
        ),
        UniqueConstraint(
            "market", "idempotency_key", name="uq_calendar_idempotency_key"
        ),
        CheckConstraint("version_number > 0", name="version_number_positive"),
        Index("ix_trading_calendar_version_market_created", "market", "created_at"),
    )

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("id", uuid4())
        super().__init__(**kwargs)

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    market: Mapped[str] = mapped_column(String(16), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_version: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    based_on_version_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("trading_calendar_version.id", ondelete="RESTRICT"),
    )
    reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    days: Mapped[list["TradingCalendarDay"]] = relationship(
        back_populates="version", order_by="TradingCalendarDay.trade_date"
    )


class TradingCalendarCurrent(Base):
    __tablename__ = "trading_calendar_current"
    __table_args__ = (
        CheckConstraint("pointer_version > 0", name="pointer_version_positive"),
    )

    market: Mapped[str] = mapped_column(String(16), primary_key=True)
    version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("trading_calendar_version.id", ondelete="RESTRICT"),
        nullable=False,
    )
    pointer_version: Mapped[int] = mapped_column(Integer, nullable=False)
    switched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TradingCalendarDay(ImmutableCalendarFact, Base):
    __tablename__ = "trading_calendar_day"
    __table_args__ = (
        UniqueConstraint("version_id", "trade_date"),
        CheckConstraint(
            "status IN ('CONFIRMED','PROVISIONAL','OVERRIDDEN','MISSING')",
            name="status_valid",
        ),
        Index("ix_trading_calendar_day_date", "trade_date", "version_id"),
    )

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("id", uuid4())
        super().__init__(**kwargs)

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("trading_calendar_version.id", ondelete="RESTRICT"),
        nullable=False,
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_trading_day: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(String(500))
    override_reason: Mapped[str | None] = mapped_column(String(500))
    version: Mapped[TradingCalendarVersion] = relationship(back_populates="days")
    sessions: Mapped[list["TradingSession"]] = relationship(
        back_populates="calendar_day", order_by="TradingSession.sequence"
    )


class TradingSession(ImmutableCalendarFact, Base):
    __tablename__ = "trading_session"
    __table_args__ = (
        UniqueConstraint(
            "calendar_day_id", "sequence", name="uq_trading_session_sequence"
        ),
        UniqueConstraint(
            "calendar_day_id",
            "starts_at",
            "ends_at",
            name="uq_trading_session_time_range",
        ),
        CheckConstraint("sequence > 0", name="sequence_positive"),
        CheckConstraint("starts_at < ends_at", name="time_order_valid"),
    )

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("id", uuid4())
        super().__init__(**kwargs)

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    calendar_day_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("trading_calendar_day.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    starts_at: Mapped[time] = mapped_column(Time(timezone=False), nullable=False)
    ends_at: Mapped[time] = mapped_column(Time(timezone=False), nullable=False)
    calendar_day: Mapped[TradingCalendarDay] = relationship(back_populates="sessions")


for immutable_model in (
    TradingCalendarVersion,
    TradingCalendarDay,
    TradingSession,
):
    event.listen(immutable_model, "before_update", immutable_model._reject_mutation)
    event.listen(immutable_model, "before_delete", immutable_model._reject_mutation)
