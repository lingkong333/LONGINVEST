from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.calendar.application import CalendarApplication
from long_invest.modules.quotes.contracts import (
    QuoteCycleStatus,
    QuoteItemStatus,
    QuoteOperationAction,
    RealtimeBatchResult,
    RealtimeCheckMode,
    SignalQuoteSnapshot,
)
from long_invest.modules.quotes.repository import QuoteCycleRepository
from long_invest.modules.quotes.service import (
    _item_view,
    _summary,
)
from long_invest.platform.database.engine import Database, get_database
from long_invest.platform.errors import AppError


class QuoteApplication:
    def __init__(
        self,
        database: Database,
        *,
        runtime_factory: Callable[[], Any] | None = None,
        calendar: CalendarApplication | None = None,
        **_legacy_factories: Any,
    ) -> None:
        self._database = database
        self._runtime_factory = runtime_factory
        self._calendar = calendar or CalendarApplication(database)

    async def list_cycles(
        self,
        *,
        status: QuoteCycleStatus | None,
        page: int,
        page_size: int,
    ) -> dict[str, object]:
        try:
            async with self._database.session() as session:
                repository = QuoteCycleRepository(session)
                items = await repository.list(
                    status=status, page=page, page_size=page_size
                )
                total = await repository.count(status=status)
                return {
                    "items": [_summary(item) for item in items],
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                }
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def list_items(
        self, cycle_id: UUID, *, page: int, page_size: int
    ) -> list[object]:
        try:
            async with self._database.session() as session:
                repository = QuoteCycleRepository(session)
                if await repository.get_with_items(cycle_id) is None:
                    raise AppError(
                        code="QUOTE_CYCLE_NOT_FOUND",
                        message="行情批次不存在",
                        status_code=404,
                    )
                return [
                    _item_view(item)
                    for item in await repository.list_items(
                        cycle_id, page=page, page_size=page_size
                    )
                ]
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def allowed_actions(self) -> tuple[QuoteOperationAction, ...]:
        return (
            QuoteOperationAction.MANUAL_COLLECT,
            QuoteOperationAction.DIAGNOSE,
        )

    async def submit_manual(
        self,
        *,
        symbols: tuple[str, ...],
        timeout_seconds: int,
        idempotency_key: str,
        request_id: str,
        created_by_user_id: str,
        reason: str,
    ) -> RealtimeBatchResult:
        _operation_reason(reason)
        now = datetime.now(UTC)
        in_session = await self._calendar.is_trading_session(now)
        return await self._runtime().run(
            symbols=symbols,
            scheduled_at=now,
            mode=(
                RealtimeCheckMode.MANUAL
                if in_session
                else RealtimeCheckMode.DIAGNOSTIC
            ),
            evaluate_signals=in_session,
            timeout_seconds=timeout_seconds,
            operation_key=f"manual:{created_by_user_id}:{idempotency_key}:{request_id}",
        )

    async def submit_diagnostic(
        self,
        *,
        symbols: tuple[str, ...],
        idempotency_key: str,
        request_id: str,
        created_by_user_id: str,
        session_id: str,
        trusted_ip: str,
        reason: str,
    ) -> RealtimeBatchResult:
        _operation_reason(reason)
        now = datetime.now(UTC)
        return await self._runtime().run(
            symbols=symbols,
            scheduled_at=now,
            mode=RealtimeCheckMode.DIAGNOSTIC,
            evaluate_signals=False,
            timeout_seconds=30,
            operation_key=(
                f"diagnose:{created_by_user_id}:{session_id}:{trusted_ip}:"
                f"{idempotency_key}:{request_id}"
            ),
        )

    def _runtime(self) -> Any:
        if self._runtime_factory is None:
            from long_invest.bootstrap.realtime_quotes import (
                get_realtime_quote_runtime,
            )

            self._runtime_factory = get_realtime_quote_runtime
        return self._runtime_factory()


class TransactionalQuoteSignalPort:
    """Public quote reader for callers that own the database transaction."""

    def __init__(
        self,
        session: Any,
        *,
        repository_factory: Callable[[Any], Any] = QuoteCycleRepository,
    ) -> None:
        self._repository = repository_factory(session)

    async def get_quote_snapshot(
        self,
        *,
        item_id: UUID,
        cycle_id: UUID,
    ) -> SignalQuoteSnapshot | None:
        item = await self._repository.get_signal_item(
            item_id=item_id,
            cycle_id=cycle_id,
        )
        if item is None:
            return None
        return SignalQuoteSnapshot(
            cycle_id=item.cycle_id,
            item_id=item.id,
            symbol=item.symbol,
            status=QuoteItemStatus(item.status),
            price=item.price,
            quote_time=item.quote_time,
            scheduled_at=item.cycle.scheduled_at,
            eligible_for_evaluation=item.eligible_for_evaluation,
            expected_subscription_version=item.expected_subscription_version,
        )


def transactional_quote_signal_port(
    session: Any,
    **factories: Any,
) -> TransactionalQuoteSignalPort:
    return TransactionalQuoteSignalPort(session, **factories)


def get_quote_application() -> QuoteApplication:
    return QuoteApplication(get_database())


def _backend_unavailable() -> AppError:
    return AppError(
        code="QUOTE_BACKEND_UNAVAILABLE",
        message="实时行情服务暂时不可用",
        status_code=503,
    )


def _operation_reason(reason: str) -> str:
    normalized = reason.strip()
    if not normalized or len(normalized) > 500:
        raise AppError(
            code="QUOTE_OPERATION_REASON_INVALID",
            message="行情操作原因必须为 1 到 500 个字符",
            status_code=422,
        )
    return normalized
