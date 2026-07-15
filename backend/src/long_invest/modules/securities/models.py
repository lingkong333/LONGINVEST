from datetime import date, datetime
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
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from long_invest.modules.securities.contracts import ListingStatus
from long_invest.platform.database.base import Base


class Security(Base):
    __tablename__ = "security"
    __table_args__ = (
        CheckConstraint("market IN ('SH','SZ','BJ','HK','US')", name="market_valid"),
        CheckConstraint(
            "security_type IN ('A_SHARE','ETF','CONVERTIBLE_BOND','B_SHARE',"
            "'FUND','INDEX','HK_STOCK','US_STOCK')",
            name="security_type_valid",
        ),
        CheckConstraint(
            "listing_status IN ('LISTED','SUSPENDED','DELISTED','DATA_MISSING')",
            name="listing_status_valid",
        ),
        CheckConstraint("master_version > 0", name="master_version_positive"),
        Index("ix_security_name_symbol", "name", "symbol"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    exchange_code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    market: Mapped[str] = mapped_column(String(8), nullable=False)
    security_type: Mapped[str] = mapped_column(String(32), nullable=False)
    listed_on: Mapped[date | None] = mapped_column(Date)
    delisted_on: Mapped[date | None] = mapped_column(Date)
    listing_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ListingStatus.LISTED
    )
    is_st: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_suspended: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    provider_codes: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    master_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_version: Mapped[str] = mapped_column(String(160), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SecurityMasterVersion(Base):
    __tablename__ = "security_master_version"
    __table_args__ = (
        UniqueConstraint("source", "source_version"),
        UniqueConstraint("source", "idempotency_key"),
        UniqueConstraint("master_version"),
        CheckConstraint("master_version > 0", name="master_version_positive"),
        CheckConstraint("item_count > 0", name="item_count_positive"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_version: Mapped[str] = mapped_column(String(160), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    master_version: Mapped[int] = mapped_column(Integer, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SecurityRevision(Base):
    __tablename__ = "security_revision"
    __table_args__ = (
        UniqueConstraint("security_id", "revision_no"),
        CheckConstraint("revision_no > 0", name="revision_no_positive"),
        CheckConstraint("master_version > 0", name="master_version_positive"),
        Index("ix_security_revision_master_version", "master_version"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    security_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("security.id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    master_version: Mapped[int] = mapped_column(Integer, nullable=False)
    changed_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    before_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    after_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SecurityUniverseSnapshot(Base):
    __tablename__ = "security_universe_snapshot"
    __table_args__ = (
        CheckConstraint("item_count >= 0", name="item_count_nonnegative"),
        CheckConstraint("master_version >= 0", name="master_version_nonnegative"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    master_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    items: Mapped[list["SecurityUniverseSnapshotItem"]] = relationship(
        back_populates="snapshot",
        order_by="SecurityUniverseSnapshotItem.symbol",
        lazy="raise",
    )


class SecurityUniverseSnapshotItem(Base):
    __tablename__ = "security_universe_snapshot_item"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "symbol"),
        CheckConstraint("master_version > 0", name="master_version_positive"),
        Index("ix_security_universe_item_symbol", "symbol"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("security_universe_snapshot.id", ondelete="RESTRICT"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    market: Mapped[str] = mapped_column(String(8), nullable=False)
    security_type: Mapped[str] = mapped_column(String(32), nullable=False)
    listing_status: Mapped[str] = mapped_column(String(32), nullable=False)
    is_st: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_suspended: Mapped[bool] = mapped_column(Boolean, nullable=False)
    master_version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[SecurityUniverseSnapshot] = relationship(back_populates="items")
