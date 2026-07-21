# ruff: noqa: E501
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from long_invest.platform.database.base import Base


class StrategyVersion(Base):
    __tablename__ = "strategy_version"
    __table_args__ = (
        UniqueConstraint("strategy_id", "version_no", name="version_number"),
        CheckConstraint("version_no > 0", name="version_positive"),
        CheckConstraint(
            "length(source_code_hash) = 64", name="source_code_hash_sha256"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    strategy_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_code: Mapped[str] = mapped_column(String, nullable=False)
    parameter_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    environment_version: Mapped[str] = mapped_column(String(64), nullable=False)
    image_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    git_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Strategy(Base):
    __tablename__ = "strategy"
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class StrategyDraft(Base):
    __tablename__ = "strategy_draft"
    __table_args__ = (UniqueConstraint("strategy_id", name="strategy"),)
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    strategy_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("strategy.id"), nullable=False
    )
    source_code: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class StrategyDraftRevision(Base):
    __tablename__ = "strategy_draft_revision"
    __table_args__ = (
        UniqueConstraint("draft_id", "revision_no", name="draft_revision"),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    draft_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("strategy_draft.id"), nullable=False
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source_code: Mapped[str] = mapped_column(String, nullable=False)


class StrategyValidationRun(Base):
    __tablename__ = "strategy_validation_run"
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    strategy_version_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))


class StrategyRun(Base):
    __tablename__ = "strategy_run"
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    strategy_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
