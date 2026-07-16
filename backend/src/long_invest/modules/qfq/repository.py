from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from hashlib import blake2b
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.qfq.models import (
    QfqDataset,
    QfqDatasetBar,
    QfqRefreshRun,
)
from long_invest.platform.errors import AppError


class QfqRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim_run(self, candidate: QfqRefreshRun) -> tuple[QfqRefreshRun, bool]:
        try:
            async with self.session.begin_nested():
                self.session.add(candidate)
                await self.session.flush()
            return candidate, True
        except IntegrityError:
            existing = await self.session.scalar(
                select(QfqRefreshRun).where(
                    QfqRefreshRun.security_id == candidate.security_id,
                    QfqRefreshRun.idempotency_key == candidate.idempotency_key,
                )
            )
            if existing is None:
                existing = await self.session.scalar(
                    select(QfqRefreshRun).where(
                        QfqRefreshRun.security_id == candidate.security_id,
                        QfqRefreshRun.request_hash == candidate.request_hash,
                    )
                )
            if existing is None:
                raise
            if existing.request_hash != candidate.request_hash:
                raise _conflict("幂等键已用于不同的前复权刷新请求") from None
            return existing, False

    async def get_run(
        self, run_id: UUID, *, for_update: bool = False
    ) -> QfqRefreshRun | None:
        statement = select(QfqRefreshRun).where(QfqRefreshRun.id == run_id)
        if for_update:
            statement = statement.with_for_update().execution_options(
                populate_existing=True
            )
        return await self.session.scalar(statement)

    async def transition_run(
        self,
        run_id: UUID,
        *,
        expected_status: str,
        status: str,
        **changes: object,
    ) -> QfqRefreshRun:
        transitioned = await self.session.scalar(
            update(QfqRefreshRun)
            .where(
                QfqRefreshRun.id == run_id,
                QfqRefreshRun.status == expected_status,
            )
            .values(status=status, **changes)
            .returning(QfqRefreshRun.id)
        )
        if transitioned is None:
            raise _conflict("前复权刷新状态已变化")
        run = await self.get_run(run_id)
        if run is None:  # pragma: no cover - guarded by the successful UPDATE
            raise _conflict("前复权刷新记录不存在")
        return run

    async def lock_security(self, security_id: UUID) -> None:
        raw_key = f"qfq:{security_id}".encode("ascii")
        lock_key = int.from_bytes(
            blake2b(raw_key, digest_size=8).digest(), "big", signed=True
        )
        await self.session.scalar(select(func.pg_advisory_xact_lock(lock_key)))

    async def lock_request(self, security_id: UUID, request_hash: str) -> None:
        raw_key = f"qfq-request:{security_id}:{request_hash}".encode("ascii")
        lock_key = int.from_bytes(
            blake2b(raw_key, digest_size=8).digest(), "big", signed=True
        )
        await self.session.scalar(select(func.pg_advisory_xact_lock(lock_key)))

    async def find_run_by_request_hash(
        self, security_id: UUID, request_hash: str
    ) -> QfqRefreshRun | None:
        return await self.session.scalar(
            select(QfqRefreshRun).where(
                QfqRefreshRun.security_id == security_id,
                QfqRefreshRun.request_hash == request_hash,
            )
        )

    async def current_dataset(
        self, security_id: UUID, *, for_update: bool = False
    ) -> QfqDataset | None:
        statement = select(QfqDataset).where(
            QfqDataset.security_id == security_id,
            QfqDataset.lifecycle == "CURRENT",
        )
        if for_update:
            statement = statement.with_for_update().execution_options(
                populate_existing=True
            )
        return await self.session.scalar(statement)

    async def get_dataset(self, dataset_id: UUID) -> QfqDataset | None:
        return await self.session.get(QfqDataset, dataset_id)

    async def next_version(self, security_id: UUID) -> int:
        latest = await self.session.scalar(
            select(func.max(QfqDataset.version)).where(
                QfqDataset.security_id == security_id
            )
        )
        return int(latest or 0) + 1

    async def add_dataset(
        self, dataset: QfqDataset, bars: Sequence[QfqDatasetBar]
    ) -> None:
        self.session.add(dataset)
        self.session.add_all(bars)
        await self.session.flush()

    async def transition_dataset(
        self,
        dataset_id: UUID,
        *,
        expected_lifecycle: str,
        lifecycle: str,
        **changes: object,
    ) -> None:
        transitioned = await self.session.scalar(
            update(QfqDataset)
            .where(
                QfqDataset.id == dataset_id,
                QfqDataset.lifecycle == expected_lifecycle,
            )
            .values(lifecycle=lifecycle, **changes)
            .returning(QfqDataset.id)
        )
        if transitioned is None:
            raise _conflict("前复权数据集状态已变化")

    async def mark_current_stale(
        self, security_id: UUID, *, reason: str
    ) -> QfqDataset | None:
        dataset_id = await self.session.scalar(
            update(QfqDataset)
            .where(
                QfqDataset.security_id == security_id,
                QfqDataset.lifecycle == "CURRENT",
            )
            .values(freshness="STALE", stale_reason=reason)
            .returning(QfqDataset.id)
        )
        return await self.get_dataset(dataset_id) if dataset_id is not None else None

    async def list_current_bars(
        self,
        dataset_id: UUID,
        *,
        start: date,
        end: date,
        page: int,
        page_size: int,
    ) -> list[QfqDatasetBar]:
        rows = await self.session.scalars(
            select(QfqDatasetBar)
            .where(
                QfqDatasetBar.dataset_id == dataset_id,
                QfqDatasetBar.trade_date.between(start, end),
            )
            .order_by(QfqDatasetBar.trade_date)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows)

    async def count_current_bars(
        self, dataset_id: UUID, *, start: date, end: date
    ) -> int:
        count = await self.session.scalar(
            select(func.count())
            .select_from(QfqDatasetBar)
            .where(
                QfqDatasetBar.dataset_id == dataset_id,
                QfqDatasetBar.trade_date.between(start, end),
            )
        )
        return int(count or 0)

    async def list_refresh_history(
        self, security_id: UUID, *, page: int, page_size: int
    ) -> list[QfqRefreshRun]:
        rows = await self.session.scalars(
            select(QfqRefreshRun)
            .where(QfqRefreshRun.security_id == security_id)
            .order_by(QfqRefreshRun.created_at.desc(), QfqRefreshRun.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows)

    async def cleanup_candidates(
        self, security_id: UUID, *, keep_versions: int = 2
    ) -> list[QfqDataset]:
        rows = await self.session.scalars(
            select(QfqDataset)
            .where(
                QfqDataset.security_id == security_id,
                QfqDataset.lifecycle == "SUPERSEDED",
            )
            .order_by(QfqDataset.version.desc())
            .offset(max(keep_versions - 1, 0))
        )
        return list(rows)

    async def flush(self) -> None:
        await self.session.flush()


def _conflict(message: str) -> AppError:
    return AppError(code="QFQ_REFRESH_CONFLICT", message=message, status_code=409)
