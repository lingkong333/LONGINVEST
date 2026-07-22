from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
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

from long_invest.modules.market_data.contracts import (
    QualityIssueStatus,
    QualitySeverity,
)
from long_invest.platform.database.base import Base


class CorporateActionFetchBatch(Base):
    __tablename__ = "corporate_action_fetch_batch"
    __table_args__ = (
        CheckConstraint("coverage_start <= coverage_end", name="coverage_window_valid"),
        CheckConstraint("observed_at <= fetched_at", name="timestamps_ordered"),
        CheckConstraint("row_count >= 0", name="row_count_non_negative"),
        CheckConstraint("status IN ('SUCCESS','FAILED')", name="status_valid"),
        CheckConstraint(
            "(status = 'SUCCESS' AND error_code IS NULL) OR "
            "(status = 'FAILED' AND error_code IS NOT NULL AND row_count = 0)",
            name="result_consistent",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_sha256"
        ),
        Index(
            "ix_corporate_action_fetch_batch_coverage",
            "security_id",
            "status",
            "observed_at",
            "coverage_start",
            "coverage_end",
        ),
    )

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("id", uuid4())
        super().__init__(**kwargs)

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    security_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("security.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    coverage_start: Mapped[date] = mapped_column(Date, nullable=False)
    coverage_end: Mapped[date] = mapped_column(Date, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CorporateActionFact(Base):
    __tablename__ = "corporate_action_fact"
    __table_args__ = (
        UniqueConstraint(
            "security_id",
            "source",
            "source_event_id",
            "revision_no",
            name="uq_corporate_action_fact_source_event_revision",
        ),
        UniqueConstraint(
            "security_id",
            "source",
            "source_event_id",
            "raw_content_hash",
            name="uq_corporate_action_fact_source_event_content",
        ),
        CheckConstraint("event_date <= effective_date", name="event_dates_ordered"),
        CheckConstraint("published_at <= observed_at", name="publication_observed"),
        CheckConstraint("revision_no > 0", name="revision_positive"),
        CheckConstraint(
            "adjustment_factor > 0 AND adjustment_factor <> 'NaN'::numeric "
            "AND adjustment_factor < 'Infinity'::numeric",
            name="factor_positive",
        ),
        CheckConstraint(
            "raw_content_hash ~ '^[0-9a-f]{64}$'", name="raw_hash_sha256"
        ),
        Index(
            "ix_corporate_action_fact_batch_effective",
            "batch_id",
            "effective_date",
        ),
        Index(
            "ix_corporate_action_fact_source_revision",
            "security_id",
            "source",
            "source_event_id",
            "revision_no",
        ),
    )

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("id", uuid4())
        super().__init__(**kwargs)

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("corporate_action_fetch_batch.id", ondelete="RESTRICT"),
        nullable=False,
    )
    security_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("security.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    adjustment_factor: Mapped[Decimal] = mapped_column(Numeric(30, 18), nullable=False)
    source_reference: Mapped[str] = mapped_column(String(500), nullable=False)
    raw_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class DataQualityIssue(Base):
    __tablename__ = "data_quality_issue"
    __table_args__ = (
        UniqueConstraint(
            "dedupe_key",
            name="uq_data_quality_issue_dedupe_key",
        ),
        CheckConstraint(
            "occurrence_count > 0",
            name="occurrence_count_positive",
        ),
        CheckConstraint(
            "status IN ('OPEN','REVIEW_REQUIRED','RESOLVED','INVALIDATED')",
            name="status_valid",
        ),
        CheckConstraint(
            "severity IN ('INFO','WARNING','ERROR','CRITICAL')",
            name="severity_valid",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence) = 'object' AND evidence <> '{}'::jsonb",
            name="evidence_non_empty_object",
        ),
        Index(
            "ix_data_quality_issue_status_last_seen",
            "status",
            "last_seen_at",
        ),
        Index(
            "ix_data_quality_issue_symbol_status",
            "symbol",
            "status",
        ),
    )

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("id", uuid4())
        super().__init__(**kwargs)

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    issue_type: Mapped[str] = mapped_column(String(100), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=QualityIssueStatus.OPEN,
    )
    severity: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=QualitySeverity.WARNING,
    )
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by_user_id: Mapped[str | None] = mapped_column(String(64))
    resolution_action: Mapped[str | None] = mapped_column(String(32))
    resolution_reason: Mapped[str | None] = mapped_column(String(500))
    selected_source: Mapped[str | None] = mapped_column(String(64))
