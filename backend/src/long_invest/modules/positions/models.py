from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from long_invest.platform.database.base import Base


class ImmutablePositionFact:
    @staticmethod
    def _reject_mutation(_mapper: object, _connection: object, _target: object) -> None:
        raise TypeError("position history is immutable")


class UserPosition(Base):
    __tablename__ = "user_position"
    __table_args__ = (
        UniqueConstraint("security_id", name="uq_user_position_security"),
        CheckConstraint("status IN ('HOLDING','NOT_HOLDING')", name="status_valid"),
        CheckConstraint("version > 0", name="version_positive"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    security_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("security.id", ondelete="RESTRICT"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    latest_history_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("user_position_history.id", ondelete="RESTRICT"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class UserPositionHistory(ImmutablePositionFact, Base):
    __tablename__ = "user_position_history"
    __table_args__ = (
        UniqueConstraint(
            "security_id",
            "position_version",
            name="uq_user_position_history_security_version",
        ),
        UniqueConstraint(
            "security_id",
            "idempotency_key",
            name="uq_user_position_history_idempotency",
        ),
        CheckConstraint(
            "before_status IS NULL OR before_status IN ('HOLDING','NOT_HOLDING')",
            name="before_status_valid",
        ),
        CheckConstraint(
            "after_status IN ('HOLDING','NOT_HOLDING')", name="after_status_valid"
        ),
        CheckConstraint("position_version > 0", name="position_version_positive"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    position_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("user_position.id", ondelete="RESTRICT"),
        nullable=False,
    )
    security_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("security.id", ondelete="RESTRICT"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    before_status: Mapped[str | None] = mapped_column(String(16))
    after_status: Mapped[str] = mapped_column(String(16), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    note: Mapped[str | None] = mapped_column(String(500))
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    position_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


event.listen(
    UserPositionHistory,
    "before_update",
    UserPositionHistory._reject_mutation,
)
event.listen(
    UserPositionHistory,
    "before_delete",
    UserPositionHistory._reject_mutation,
)
