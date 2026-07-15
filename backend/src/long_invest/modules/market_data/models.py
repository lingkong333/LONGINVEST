from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
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

from long_invest.modules.market_data.contracts import (
    QualityIssueStatus,
    QualitySeverity,
)
from long_invest.platform.database.base import Base


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
