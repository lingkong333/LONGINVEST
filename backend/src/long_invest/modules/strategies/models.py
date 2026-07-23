from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
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
from sqlalchemy.orm import Mapped, mapped_column

from long_invest.platform.database.base import Base


class StrategyVersion(Base):
    __tablename__ = "strategy_version"
    __table_args__ = (
        UniqueConstraint(
            "strategy_id",
            "version_no",
            name="uq_strategy_version_strategy_id_version_no",
        ),
        CheckConstraint("version_no > 0", name="version_positive"),
        CheckConstraint(
            "source_code_hash ~ '^[0-9a-f]{64}$'",
            name="source_code_hash_sha256",
        ),
        CheckConstraint(
            "runner_image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="runner_image_digest_sha256",
        ),
        CheckConstraint(
            "status IN ('PUBLISHING','PUBLISHED','PUBLISH_FAILED','ARCHIVED')",
            name="status_valid",
        ),
        CheckConstraint(
            "(status IN ('PUBLISHED','ARCHIVED') AND published_at IS NOT NULL "
            "AND git_commit IS NOT NULL AND validation_run_id IS NOT NULL) OR "
            "(status IN ('PUBLISHING','PUBLISH_FAILED') AND published_at IS NULL)",
            name="publication_consistent",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    strategy_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("strategy.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_code: Mapped[str] = mapped_column(String, nullable=False)
    strategy_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False
    )
    parameter_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    environment_version: Mapped[str] = mapped_column(String(64), nullable=False)
    runner_image_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    git_commit: Mapped[str | None] = mapped_column(String(64))
    validation_run_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("strategy_validation_run.id", ondelete="RESTRICT"),
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Strategy(Base):
    __tablename__ = "strategy"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT','VALIDATING','VALIDATED','PUBLISHING',"
            "'PUBLISHED','PUBLISH_FAILED','ARCHIVED')",
            name="status_valid",
        ),
        Index("ix_strategy_status", "status"),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class StrategyDraft(Base):
    __tablename__ = "strategy_draft"
    __table_args__ = (
        UniqueConstraint("strategy_id", name="uq_strategy_draft_strategy_id"),
        CheckConstraint("draft_version > 0", name="version_positive"),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    strategy_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("strategy.id"), nullable=False
    )
    source_code: Mapped[str] = mapped_column(String, nullable=False)
    strategy_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    parameter_schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    draft_version: Mapped[int] = mapped_column(Integer, nullable=False)


class StrategyDraftRevision(Base):
    __tablename__ = "strategy_draft_revision"
    __table_args__ = (
        UniqueConstraint(
            "draft_id",
            "revision_no",
            name="uq_strategy_draft_revision_draft_id_revision_no",
        ),
        CheckConstraint("revision_no > 0", name="revision_positive"),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    draft_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("strategy_draft.id"), nullable=False
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source_code: Mapped[str] = mapped_column(String, nullable=False)
    strategy_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    parameter_schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )


class StrategyValidationRun(Base):
    __tablename__ = "strategy_validation_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED')",
            name="status_valid",
        ),
        CheckConstraint("draft_version > 0", name="draft_version_positive"),
        CheckConstraint(
            "source_code_hash ~ '^[0-9a-f]{64}$'",
            name="source_code_hash_sha256",
        ),
        CheckConstraint(
            "(status IN ('PENDING','RUNNING') AND completed_at IS NULL "
            "AND error_code IS NULL) OR "
            "(status = 'SUCCEEDED' AND completed_at IS NOT NULL "
            "AND error_code IS NULL) OR "
            "(status = 'FAILED' AND completed_at IS NOT NULL "
            "AND error_code IS NOT NULL)",
            name="completion_consistent",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="completion_time_valid",
        ),
        Index("ix_strategy_validation_run_status", "status"),
        Index(
            "ix_strategy_validation_run_draft_evidence",
            "strategy_id",
            "draft_version",
            "source_code_hash",
            "status",
        ),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    strategy_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("strategy.id", ondelete="RESTRICT"),
        nullable=False,
    )
    strategy_version_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("strategy_version.id", ondelete="RESTRICT"),
    )
    draft_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StrategyRun(Base):
    __tablename__ = "strategy_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELED')",
            name="status_valid",
        ),
        Index(
            "ix_strategy_run_strategy_version_status",
            "strategy_version_id",
            "status",
        ),
    )
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    strategy_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("strategy_version.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
