from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.daily_data.contracts import CreateDailyBatch, StageDailyBar
from long_invest.modules.daily_data.models import (
    DailyBarRevision,
    DailyBarStage,
    DailyBarUnadjusted,
    DailyBatchMissingItem,
    DailyDataBatch,
)
from long_invest.platform.errors import AppError


class DailyDataRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim_batch(
        self, command: CreateDailyBatch, now: datetime
    ) -> tuple[DailyDataBatch, bool]:
        existing = await self.session.scalar(
            select(DailyDataBatch).where(
                DailyDataBatch.idempotency_key == command.idempotency_key
            )
        )
        if existing is not None:
            _validate_batch_replay(existing, command)
            return existing, False
        if command.parent_batch_id is not None:
            parent = await self.session.scalar(
                select(DailyDataBatch)
                .where(DailyDataBatch.id == command.parent_batch_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            _validate_retry_parent(parent, command)
        candidate = DailyDataBatch(
            id=uuid4(),
            trading_date=command.trading_date,
            universe_snapshot_id=command.universe_snapshot_id,
            parent_batch_id=command.parent_batch_id,
            symbols=list(command.symbols),
            idempotency_key=command.idempotency_key,
            status="PENDING",
            expected_count=len(command.symbols),
            fetched_count=0,
            validated_count=0,
            committed_count=0,
            missing_count=0,
            failed_count=0,
            created_at=now,
            deadline_at=command.deadline_at,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(candidate)
                await self.session.flush()
            return candidate, True
        except IntegrityError:
            existing_by_key = await self.session.scalar(
                select(DailyDataBatch).where(
                    DailyDataBatch.idempotency_key == command.idempotency_key
                )
            )
            if existing_by_key is not None:
                _validate_batch_replay(existing_by_key, command)
                return existing_by_key, False
            if command.parent_batch_id is not None:
                raise
            existing_scope = await self.session.scalar(
                select(DailyDataBatch).where(
                    DailyDataBatch.trading_date == command.trading_date,
                    DailyDataBatch.universe_snapshot_id == command.universe_snapshot_id,
                    DailyDataBatch.parent_batch_id.is_(None),
                )
            )
            if existing_scope is None:
                raise
            _validate_scope_replay(existing_scope, command)
            return existing_scope, False

    async def get_batch(
        self, batch_id: UUID, *, for_update: bool = False
    ) -> DailyDataBatch | None:
        statement = select(DailyDataBatch).where(DailyDataBatch.id == batch_id)
        if for_update:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def upsert_stage(
        self,
        batch_id: UUID,
        item: StageDailyBar,
        expires_at: datetime,
    ) -> DailyBarStage:
        values = {
            "id": uuid4(),
            "batch_id": batch_id,
            "security_id": item.security_id,
            "symbol": item.symbol,
            "trading_date": item.trading_date,
            "status": item.status.value,
            "provider_payload": _json_value(dict(item.provider_payload or {})) or None,
            "missing_reason": item.missing_reason.value
            if item.missing_reason
            else None,
            "error_code": item.error_code,
            "quality_code": item.quality_code,
            "received_at": item.received_at,
            "expires_at": expires_at,
        }
        statement = (
            insert(DailyBarStage)
            .values(**values)
            .on_conflict_do_update(
                constraint="uq_daily_stage_symbol",
                set_={key: value for key, value in values.items() if key != "id"},
            )
            .returning(DailyBarStage)
        )
        return (await self.session.execute(statement)).scalar_one()

    async def list_stages(self, batch_id: UUID) -> list[DailyBarStage]:
        result = await self.session.scalars(
            select(DailyBarStage)
            .where(DailyBarStage.batch_id == batch_id)
            .order_by(DailyBarStage.symbol)
        )
        return list(result)

    async def list_all_missing(self, batch_id: UUID) -> list[DailyBatchMissingItem]:
        result = await self.session.scalars(
            select(DailyBatchMissingItem)
            .where(DailyBatchMissingItem.batch_id == batch_id)
            .order_by(DailyBatchMissingItem.symbol)
        )
        return list(result)

    async def replace_missing(
        self, batch_id: UUID, items: Sequence[DailyBatchMissingItem]
    ) -> None:
        await self.session.execute(
            delete(DailyBatchMissingItem).where(
                DailyBatchMissingItem.batch_id == batch_id
            )
        )
        self.session.add_all(items)
        await self.session.flush()

    async def get_bar(
        self, security_id: UUID, trade_date: date
    ) -> DailyBarUnadjusted | None:
        return await self.session.get(DailyBarUnadjusted, (security_id, trade_date))

    async def add_bar(self, bar: DailyBarUnadjusted) -> None:
        self.session.add(bar)
        await self.session.flush()

    async def next_revision_no(self, security_id: UUID, trade_date: date) -> int:
        value = await self.session.scalar(
            select(func.max(DailyBarRevision.revision_no)).where(
                DailyBarRevision.daily_bar_security_id == security_id,
                DailyBarRevision.daily_bar_trade_date == trade_date,
            )
        )
        return int(value or 0) + 1

    async def add_revision(self, revision: DailyBarRevision) -> None:
        self.session.add(revision)
        await self.session.flush()

    @asynccontextmanager
    async def item_savepoint(self) -> AsyncIterator[None]:
        async with self.session.begin_nested():
            yield

    async def flush(self) -> None:
        await self.session.flush()

    async def list_batches(self, *, page: int, page_size: int) -> list[DailyDataBatch]:
        result = await self.session.scalars(
            select(DailyDataBatch)
            .order_by(
                DailyDataBatch.trading_date.desc(), DailyDataBatch.created_at.desc()
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(result)

    async def count_batches(self) -> int:
        return int(
            await self.session.scalar(select(func.count(DailyDataBatch.id))) or 0
        )

    async def list_missing(
        self, batch_id: UUID, *, page: int, page_size: int
    ) -> list[DailyBatchMissingItem]:
        result = await self.session.scalars(
            select(DailyBatchMissingItem)
            .where(DailyBatchMissingItem.batch_id == batch_id)
            .order_by(DailyBatchMissingItem.symbol)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(result)

    async def count_missing(self, batch_id: UUID) -> int:
        return int(
            await self.session.scalar(
                select(func.count(DailyBatchMissingItem.id)).where(
                    DailyBatchMissingItem.batch_id == batch_id
                )
            )
            or 0
        )

    async def list_bars(
        self,
        symbol: str,
        *,
        start: date,
        end: date,
        page: int,
        page_size: int,
    ) -> list[DailyBarUnadjusted]:
        result = await self.session.scalars(
            select(DailyBarUnadjusted)
            .where(
                DailyBarUnadjusted.symbol == symbol,
                DailyBarUnadjusted.trade_date.between(start, end),
            )
            .order_by(DailyBarUnadjusted.trade_date)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(result)

    async def count_bars(self, symbol: str, *, start: date, end: date) -> int:
        return int(
            await self.session.scalar(
                select(func.count())
                .select_from(DailyBarUnadjusted)
                .where(
                    DailyBarUnadjusted.symbol == symbol,
                    DailyBarUnadjusted.trade_date.between(start, end),
                )
            )
            or 0
        )

    async def list_revisions(
        self, symbol: str, *, page: int, page_size: int
    ) -> list[DailyBarRevision]:
        result = await self.session.scalars(
            select(DailyBarRevision)
            .where(DailyBarRevision.symbol == symbol)
            .order_by(
                DailyBarRevision.daily_bar_trade_date.desc(),
                DailyBarRevision.revision_no.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(result)

    async def count_revisions(self, symbol: str) -> int:
        return int(
            await self.session.scalar(
                select(func.count(DailyBarRevision.id)).where(
                    DailyBarRevision.symbol == symbol
                )
            )
            or 0
        )


def _validate_batch_replay(existing: DailyDataBatch, command: CreateDailyBatch) -> None:
    if (
        existing.trading_date != command.trading_date
        or existing.universe_snapshot_id != command.universe_snapshot_id
        or tuple(existing.symbols) != command.symbols
        or existing.parent_batch_id != command.parent_batch_id
    ):
        raise AppError(
            code="DAILY_BATCH_IDEMPOTENCY_CONFLICT",
            message="该幂等键已用于不同的日线批次请求",
            status_code=409,
        )


def _validate_scope_replay(existing: DailyDataBatch, command: CreateDailyBatch) -> None:
    if (
        existing.trading_date != command.trading_date
        or existing.universe_snapshot_id != command.universe_snapshot_id
        or existing.parent_batch_id is not None
        or command.parent_batch_id is not None
        or tuple(existing.symbols) != command.symbols
    ):
        raise AppError(
            code="DAILY_BATCH_SCOPE_CONFLICT",
            message="同一交易日和范围快照已有不同的自动日线批次",
            status_code=409,
        )


def _validate_retry_parent(
    parent: DailyDataBatch | None,
    command: CreateDailyBatch,
) -> None:
    if parent is None:
        raise AppError(
            code="DAILY_PARENT_BATCH_NOT_FOUND",
            message="原日线批次不存在",
            status_code=404,
        )
    if parent.status not in {"PARTIAL", "FAILED"}:
        raise AppError(
            code="DAILY_PARENT_BATCH_STATE_CONFLICT",
            message="原日线批次当前状态不允许重试",
            status_code=409,
            details={"status": parent.status},
        )
    if (
        parent.trading_date != command.trading_date
        or parent.universe_snapshot_id != command.universe_snapshot_id
        or not set(command.symbols).issubset(parent.symbols)
    ):
        raise AppError(
            code="DAILY_RETRY_SCOPE_CONFLICT",
            message="重试范围必须属于原日线批次的同日同快照范围",
            status_code=409,
        )


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value
