from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from long_invest.modules.qfq.contracts import (
    QfqDatasetAction,
    QfqDatasetLifecycle,
    QfqFreshness,
    QfqRefreshStatus,
    ValidatedQfqWindow,
)
from long_invest.modules.qfq.models import (
    QfqDataset,
    QfqDatasetBar,
    QfqRefreshRun,
)
from long_invest.platform.errors import AppError


def qfq_dataset_allowed_actions(
    lifecycle: QfqDatasetLifecycle | str,
    *,
    refresh_in_progress: bool,
) -> tuple[QfqDatasetAction, ...]:
    if (
        QfqDatasetLifecycle(str(lifecycle)) is QfqDatasetLifecycle.CURRENT
        and not refresh_in_progress
    ):
        return (QfqDatasetAction.REFRESH,)
    return ()


class QfqRepositoryPort(Protocol):
    session: object

    async def lock_security(self, security_id: UUID) -> None: ...
    async def get_run(
        self, run_id: UUID, *, for_update: bool = False
    ) -> QfqRefreshRun | None: ...
    async def transition_run(
        self,
        run_id: UUID,
        *,
        expected_status: str,
        status: str,
        **changes: object,
    ) -> QfqRefreshRun: ...
    async def current_dataset(
        self, security_id: UUID, *, for_update: bool = False
    ) -> QfqDataset | None: ...
    async def get_dataset(self, dataset_id: UUID) -> QfqDataset | None: ...
    async def next_version(self, security_id: UUID) -> int: ...
    async def add_dataset(
        self, dataset: QfqDataset, bars: list[QfqDatasetBar]
    ) -> None: ...
    async def transition_dataset(
        self,
        dataset_id: UUID,
        *,
        expected_lifecycle: str,
        lifecycle: str,
        **changes: object,
    ) -> None: ...
    async def mark_current_stale(
        self, security_id: UUID, *, reason: str
    ) -> QfqDataset | None: ...
    async def flush(self) -> None: ...


class QfqEventPort(Protocol):
    session: object

    async def completed(self, run: QfqRefreshRun, dataset: QfqDataset) -> None: ...
    async def failed(self, run: QfqRefreshRun, current: QfqDataset | None) -> None: ...


class QfqRefreshService:
    def __init__(self, repository: QfqRepositoryPort, *, events: QfqEventPort) -> None:
        if events.session is not repository.session:
            raise AppError(
                code="QFQ_TRANSACTION_MISMATCH",
                message="前复权数据和事件必须位于同一事务",
                status_code=500,
            )
        self._repository = repository
        self._events = events

    async def activate(
        self,
        run_id: UUID,
        window: ValidatedQfqWindow,
        *,
        current_input_daily_version: int,
        provider_contract_version: str,
        now: datetime,
    ) -> QfqDataset | None:
        run = await self._required_run(run_id)
        await self._repository.lock_security(run.security_id)
        run = await self._required_run(run_id)

        if str(run.status) == QfqRefreshStatus.SUCCEEDED:
            return await self._repository.get_dataset(run.activated_dataset_id)
        if str(run.status) != QfqRefreshStatus.VALIDATING:
            raise _conflict("刷新记录当前状态不能激活数据集")

        current = await self._repository.current_dataset(
            run.security_id, for_update=True
        )
        if current_input_daily_version != run.input_daily_version:
            run = await self._repository.transition_run(
                run.id,
                expected_status=QfqRefreshStatus.VALIDATING,
                status=QfqRefreshStatus.SUPERSEDED,
                error_code="QFQ_INPUT_SUPERSEDED",
                retryable=False,
                completed_at=now,
                updated_at=now,
            )
            await self._events.failed(run, current)
            return None

        run = await self._repository.transition_run(
            run.id,
            expected_status=QfqRefreshStatus.VALIDATING,
            status=QfqRefreshStatus.COMMITTING,
            updated_at=now,
        )
        if current is not None and current.checksum == window.checksum:
            await self._repository.transition_dataset(
                current.id,
                expected_lifecycle=QfqDatasetLifecycle.CURRENT,
                lifecycle=QfqDatasetLifecycle.CURRENT,
                freshness=QfqFreshness.FRESH,
                stale_reason=None,
            )
            current.freshness = QfqFreshness.FRESH
            current.stale_reason = None
            return await self._complete(run, current, now=now)

        dataset = QfqDataset(
            id=uuid4(),
            security_id=run.security_id,
            symbol=run.symbol,
            version=await self._repository.next_version(run.security_id),
            requested_start=run.requested_start,
            requested_end=run.requested_end,
            actual_start=window.bars[0].trade_date,
            actual_end=window.bars[-1].trade_date,
            as_of_date=run.as_of_date,
            provider=run.provider,
            provider_contract_version=provider_contract_version,
            anchor_date=window.anchor_date,
            anchor_close=window.anchor_close,
            row_count=window.row_count,
            checksum=window.checksum,
            lifecycle=QfqDatasetLifecycle.STAGING,
            freshness=QfqFreshness.FRESH,
            stale_reason=None,
            created_at=now,
        )
        bars = [
            QfqDatasetBar(
                dataset_id=dataset.id,
                trade_date=item.trade_date,
                open=item.open,
                high=item.high,
                low=item.low,
                close=item.close,
                volume=item.volume,
                amount=item.amount,
            )
            for item in window.bars
        ]
        await self._repository.add_dataset(dataset, bars)
        if current is not None:
            await self._repository.transition_dataset(
                current.id,
                expected_lifecycle=QfqDatasetLifecycle.CURRENT,
                lifecycle=QfqDatasetLifecycle.SUPERSEDED,
                superseded_at=now,
            )
        await self._repository.transition_dataset(
            dataset.id,
            expected_lifecycle=QfqDatasetLifecycle.STAGING,
            lifecycle=QfqDatasetLifecycle.CURRENT,
            activated_at=now,
        )
        dataset.lifecycle = QfqDatasetLifecycle.CURRENT
        dataset.activated_at = now
        return await self._complete(run, dataset, now=now)

    async def fail(
        self,
        run_id: UUID,
        *,
        code: str,
        retryable: bool,
        now: datetime,
    ) -> QfqDataset | None:
        run = await self._required_run(run_id)
        await self._repository.lock_security(run.security_id)
        run = await self._required_run(run_id)
        if str(run.status) in {
            QfqRefreshStatus.FAILED,
            QfqRefreshStatus.TIMED_OUT,
            QfqRefreshStatus.SUPERSEDED,
        }:
            return await self._repository.current_dataset(
                run.security_id, for_update=True
            )
        if str(run.status) not in {
            QfqRefreshStatus.PENDING,
            QfqRefreshStatus.FETCHING,
            QfqRefreshStatus.VALIDATING,
            QfqRefreshStatus.COMMITTING,
        }:
            raise _conflict("刷新记录当前状态不能标记失败")
        current = await self._repository.mark_current_stale(
            run.security_id, reason=code
        )
        terminal_status = (
            QfqRefreshStatus.TIMED_OUT
            if code == "QFQ_REFRESH_TIMED_OUT"
            else QfqRefreshStatus.FAILED
        )
        run = await self._repository.transition_run(
            run.id,
            expected_status=str(run.status),
            status=terminal_status,
            error_code=code,
            retryable=retryable,
            completed_at=now,
            updated_at=now,
        )
        await self._events.failed(run, current)
        return current

    async def _complete(
        self, run: QfqRefreshRun, dataset: QfqDataset, *, now: datetime
    ) -> QfqDataset:
        run = await self._repository.transition_run(
            run.id,
            expected_status=QfqRefreshStatus.COMMITTING,
            status=QfqRefreshStatus.SUCCEEDED,
            candidate_dataset_id=dataset.id,
            activated_dataset_id=dataset.id,
            row_count=dataset.row_count,
            checksum=dataset.checksum,
            error_code=None,
            retryable=None,
            completed_at=now,
            updated_at=now,
        )
        await self._events.completed(run, dataset)
        return dataset

    async def _required_run(self, run_id: UUID) -> QfqRefreshRun:
        run = await self._repository.get_run(run_id, for_update=True)
        if run is None:
            raise AppError(
                code="QFQ_REFRESH_NOT_FOUND",
                message="前复权刷新记录不存在",
                status_code=404,
            )
        return run


def _conflict(message: str) -> AppError:
    return AppError(code="QFQ_REFRESH_CONFLICT", message=message, status_code=409)
