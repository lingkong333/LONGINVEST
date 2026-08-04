from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
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


class StrategyScreeningBatch(Base):
    __tablename__ = "strategy_screening_batch"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_strategy_screening_batch_idempotency_key"
        ),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','PAUSING','PAUSED','SUCCEEDED',"
            "'PARTIAL','FAILED','CANCELING','CANCELED')",
            name="status_valid",
        ),
        CheckConstraint(
            "parameter_hash ~ '^[0-9a-f]{64}$'",
            name="parameter_hash_sha256",
        ),
        CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'",
            name="request_hash_sha256",
        ),
        CheckConstraint(
            "(status IN ('SUCCEEDED','PARTIAL','FAILED','CANCELED') "
            "AND completed_at IS NOT NULL) OR "
            "(status NOT IN ('SUCCEEDED','PARTIAL','FAILED','CANCELED') "
            "AND completed_at IS NULL)",
            name="completion_consistent",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="completion_time_valid",
        ),
        Index("ix_strategy_screening_batch_status_created", "status", "created_at"),
        Index(
            "ix_strategy_screening_batch_strategy_version_created",
            "strategy_version_id",
            "created_at",
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
    security_universe_snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("security_universe_snapshot.id", ondelete="RESTRICT"),
        nullable=False,
    )
    job_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("job.id", ondelete="RESTRICT"),
        unique=True,
    )
    parameter_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    parameter_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StrategyScreeningPeriod(Base):
    __tablename__ = "strategy_screening_period"
    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "sequence_no",
            name="uq_strategy_screening_period_batch_sequence",
        ),
        UniqueConstraint(
            "batch_id", "id", name="uq_strategy_screening_period_batch_id"
        ),
        CheckConstraint("sequence_no > 0", name="sequence_positive"),
        CheckConstraint(
            "training_start_date <= training_end_date "
            "AND training_end_date < test_start_date "
            "AND test_start_date <= test_end_date",
            name="date_range_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("strategy_screening_batch.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    training_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    training_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    test_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    test_end_date: Mapped[date] = mapped_column(Date, nullable=False)


class StrategyScreeningScopeItem(Base):
    __tablename__ = "strategy_screening_scope_item"
    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "security_id",
            name="uq_strategy_screening_scope_batch_security",
        ),
        UniqueConstraint(
            "batch_id", "id", name="uq_strategy_screening_scope_batch_id"
        ),
        CheckConstraint(
            "symbol ~ '^[0-9]{6}\\.(SH|SZ|BJ)$'", name="symbol_valid"
        ),
        CheckConstraint(
            "(qfq_dataset_id IS NULL AND qfq_data_version IS NULL "
            "AND qfq_data_hash IS NULL) OR "
            "(qfq_dataset_id IS NOT NULL AND qfq_data_version > 0 "
            "AND qfq_data_hash ~ '^[0-9a-f]{64}$')",
            name="qfq_snapshot_consistent",
        ),
        Index("ix_strategy_screening_scope_symbol", "batch_id", "symbol"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("strategy_screening_batch.id", ondelete="CASCADE"),
        nullable=False,
    )
    security_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("security.id", ondelete="RESTRICT"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    qfq_dataset_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("qfq_dataset.id", ondelete="RESTRICT"),
    )
    qfq_data_version: Mapped[int | None] = mapped_column(Integer)
    qfq_data_hash: Mapped[str | None] = mapped_column(String(64))


class StrategyScreeningResult(Base):
    __tablename__ = "strategy_screening_result"
    __table_args__ = (
        UniqueConstraint(
            "period_id",
            "scope_item_id",
            name="uq_strategy_screening_result_period_scope",
        ),
        ForeignKeyConstraint(
            ("batch_id", "period_id"),
            (
                "strategy_screening_period.batch_id",
                "strategy_screening_period.id",
            ),
            name="fk_strategy_screening_result_batch_period",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("batch_id", "scope_item_id"),
            (
                "strategy_screening_scope_item.batch_id",
                "strategy_screening_scope_item.id",
            ),
            name="fk_strategy_screening_result_batch_scope",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','MATCHED','NOT_MATCHED',"
            "'FAILED','CANCELED')",
            name="status_valid",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint(
            "training_row_count IS NULL OR training_row_count > 0",
            name="training_row_count_positive",
        ),
        CheckConstraint(
            "training_data_hash IS NULL "
            "OR training_data_hash ~ '^[0-9a-f]{64}$'",
            name="training_data_hash_sha256",
        ),
        CheckConstraint(
            "(status = 'MATCHED' AND low_strong IS NOT NULL "
            "AND low_watch IS NOT NULL AND high_watch IS NOT NULL "
            "AND high_strong IS NOT NULL AND low_strong > 0 "
            "AND low_strong < low_watch AND low_watch < high_watch "
            "AND high_watch < high_strong "
            "AND high_strong < 'Infinity'::numeric "
            "AND reason IS NULL AND failure_code IS NULL "
            "AND training_data_hash IS NOT NULL "
            "AND training_row_count IS NOT NULL) OR "
            "(status = 'NOT_MATCHED' AND low_strong IS NULL "
            "AND low_watch IS NULL AND high_watch IS NULL "
            "AND high_strong IS NULL AND length(btrim(reason)) > 0 "
            "AND failure_code IS NULL AND training_data_hash IS NOT NULL "
            "AND training_row_count IS NOT NULL) OR "
            "(status = 'FAILED' AND low_strong IS NULL "
            "AND low_watch IS NULL AND high_watch IS NULL "
            "AND high_strong IS NULL AND reason IS NULL "
            "AND length(btrim(failure_code)) > 0) OR "
            "(status IN ('PENDING','RUNNING','CANCELED') "
            "AND low_strong IS NULL AND low_watch IS NULL "
            "AND high_watch IS NULL AND high_strong IS NULL "
            "AND reason IS NULL AND failure_code IS NULL)",
            name="outcome_consistent",
        ),
        CheckConstraint(
            "(status IN ('MATCHED','NOT_MATCHED','FAILED','CANCELED') "
            "AND ended_at IS NOT NULL) OR "
            "(status IN ('PENDING','RUNNING') AND ended_at IS NULL)",
            name="completion_consistent",
        ),
        CheckConstraint(
            "started_at IS NULL OR ended_at IS NULL OR ended_at >= started_at",
            name="completion_time_valid",
        ),
        Index(
            "ix_strategy_screening_result_batch_status", "batch_id", "status"
        ),
        Index(
            "ix_strategy_screening_result_scope_period",
            "scope_item_id",
            "period_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    batch_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    period_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    scope_item_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    low_strong: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    low_watch: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    high_watch: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    high_strong: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    reason: Mapped[str | None] = mapped_column(String(500))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    diagnostics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    training_data_hash: Mapped[str | None] = mapped_column(String(64))
    training_row_count: Mapped[int | None] = mapped_column(Integer)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StrategyScreeningControlCommand(Base):
    __tablename__ = "strategy_screening_control_command"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_strategy_screening_control_idempotency_key",
        ),
        CheckConstraint(
            "action IN ('PAUSE','RESUME','CANCEL','RETRY_FAILED')",
            name="action_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("strategy_screening_batch.id", ondelete="RESTRICT"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
