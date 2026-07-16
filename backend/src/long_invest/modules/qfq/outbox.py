from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.qfq.models import QfqDataset, QfqRefreshRun
from long_invest.platform.outbox.service import TransactionalOutboxWriter


class TransactionBoundOutboxWriter(Protocol):
    async def append(
        self,
        *,
        session: AsyncSession,
        topic: str,
        aggregate_type: str,
        aggregate_id: str,
        queue: str,
        payload: dict[str, Any],
        dedupe_key: str,
    ) -> None: ...


class QfqEventAdapter:
    def __init__(
        self,
        session: AsyncSession,
        writer: TransactionBoundOutboxWriter | None = None,
    ) -> None:
        self.session = session
        self._writer = writer or TransactionalOutboxWriter()

    async def completed(self, run: QfqRefreshRun, dataset: QfqDataset) -> None:
        await self._append(
            run,
            topic="qfq_refresh.completed",
            payload={
                "event_type": "qfq_refresh.completed",
                "run_id": str(run.id),
                "security_id": str(run.security_id),
                "symbol": run.symbol,
                "dataset_id": str(dataset.id),
                "version": dataset.version,
                "start": run.requested_start.isoformat(),
                "end": run.requested_end.isoformat(),
                "as_of_date": run.as_of_date.isoformat(),
                "row_count": dataset.row_count,
                "checksum": dataset.checksum,
                "input_daily_version": run.input_daily_version,
                "trigger_reason": run.trigger_reason,
            },
            dedupe_key=f"qfq:{run.id}:completed",
        )

    async def failed(self, run: QfqRefreshRun, current: QfqDataset | None) -> None:
        await self._append(
            run,
            topic="qfq_refresh.failed",
            payload={
                "event_type": "qfq_refresh.failed",
                "run_id": str(run.id),
                "security_id": str(run.security_id),
                "symbol": run.symbol,
                "start": run.requested_start.isoformat(),
                "end": run.requested_end.isoformat(),
                "as_of_date": run.as_of_date.isoformat(),
                "error_code": run.error_code,
                "has_current_dataset": current is not None,
                "current_dataset_stale": current is not None
                and str(current.freshness) == "STALE",
                "retryable": run.retryable,
                "trigger_reason": run.trigger_reason,
            },
            dedupe_key=f"qfq:{run.id}:failed",
        )

    async def _append(
        self,
        run: QfqRefreshRun,
        *,
        topic: str,
        payload: dict[str, Any],
        dedupe_key: str,
    ) -> None:
        await self._writer.append(
            session=self.session,
            topic=topic,
            aggregate_type="qfq_refresh_run",
            aggregate_id=str(run.id),
            queue="domain-events",
            payload=payload,
            dedupe_key=dedupe_key,
        )
