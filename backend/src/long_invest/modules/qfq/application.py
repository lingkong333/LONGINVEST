from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from long_invest.modules.calendar.application import (
    CalendarApplication,
    get_calendar_application,
)
from long_invest.modules.daily_data.application import (
    DailyDataApplication,
    get_daily_data_application,
)
from long_invest.modules.qfq.contracts import (
    Page,
    QfqBarView,
    QfqDatasetLifecycle,
    QfqDatasetView,
    QfqFreshness,
    QfqRefreshStatus,
    QfqRefreshView,
    RefreshQfq,
    ValidatedQfqWindow,
)
from long_invest.modules.qfq.models import QfqRefreshRun
from long_invest.modules.qfq.outbox import QfqEventAdapter
from long_invest.modules.qfq.repository import QfqRepository
from long_invest.modules.qfq.service import QfqRefreshService
from long_invest.modules.securities.application import (
    SecurityApplication,
    get_security_application,
)
from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.audit.service import AuditService
from long_invest.platform.database.engine import Database, get_database
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import SubmitJob
from long_invest.platform.jobs.service import JobService


class QfqApplication:
    def __init__(
        self,
        database: Database,
        *,
        security_application: SecurityApplication,
        calendar_application: CalendarApplication,
        daily_application: DailyDataApplication,
        repository_factory: Callable[..., Any] = QfqRepository,
        job_service_factory: Callable[..., Any] = JobService,
        audit_service_factory: Callable[..., Any] = AuditService,
        event_factory: Callable[..., Any] = QfqEventAdapter,
        domain_service_factory: Callable[..., Any] = QfqRefreshService,
    ) -> None:
        self._database = database
        self._security = security_application
        self._calendar = calendar_application
        self._daily = daily_application
        self._repository_factory = repository_factory
        self._job_service_factory = job_service_factory
        self._audit_service_factory = audit_service_factory
        self._event_factory = event_factory
        self._domain_service_factory = domain_service_factory

    async def get_data(
        self,
        symbol: str,
        *,
        start: date | None,
        end: date | None,
        page: int,
        page_size: int,
    ) -> tuple[QfqDatasetView, Page[QfqBarView]]:
        if start is not None and end is not None and start > end:
            raise _window_invalid("开始日期不能晚于结束日期")
        security = await self._call_public(self._security.resolve_identity(symbol))
        try:
            async with self._database.session() as session:
                repository = self._repository_factory(session)
                dataset = await repository.current_dataset(security.security_id)
                if dataset is None:
                    raise AppError(
                        code="QFQ_DATA_NOT_FOUND",
                        message="当前股票没有可用的前复权数据",
                        status_code=404,
                    )
                wanted_start = start or dataset.actual_start
                wanted_end = end or dataset.actual_end
                if wanted_start > wanted_end:
                    raise _window_invalid("查询日期窗口无效")
                bars = await repository.list_current_bars(
                    dataset.id,
                    start=wanted_start,
                    end=wanted_end,
                    page=page,
                    page_size=page_size,
                )
                total = await repository.count_current_bars(
                    dataset.id, start=wanted_start, end=wanted_end
                )
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc
        return _dataset_view(dataset), Page(
            items=tuple(_bar_view(item) for item in bars),
            total=total,
            page=page,
            page_size=page_size,
        )

    async def submit_refresh(
        self,
        *,
        symbol: str,
        start: date,
        end: date,
        as_of_date: date,
        reason: str,
        idempotency_key: str,
        request_id: str,
        actor_user_id: str,
        session_id: str,
        trusted_ip: str,
    ) -> Any:
        reason = reason.strip()
        idempotency_key = idempotency_key.strip()
        if (
            not reason
            or len(reason) > 64
            or not idempotency_key
            or len(idempotency_key) > 160
            or start > end
            or as_of_date != end
        ):
            raise _window_invalid("刷新窗口、目标日或原因无效")
        security = await self._call_public(self._security.resolve_identity(symbol))
        window = await self._calendar_window(start, end)
        daily = await self._call_public(self._daily.snapshot(symbol, as_of_date))
        if (
            daily is None
            or daily.security_id != security.security_id
            or daily.symbol != security.symbol
            or daily.trade_date != as_of_date
        ):
            raise AppError(
                code="QFQ_DAILY_GATE_NOT_MET",
                message="目标日没有匹配的已提交不复权日线",
                status_code=409,
            )
        try:
            command = RefreshQfq(
                security_id=security.security_id,
                symbol=security.symbol,
                start=start,
                end=end,
                as_of_date=as_of_date,
                expected_trade_dates=window.dates,
                input_daily_version=daily.data_version,
                trigger_reason=reason,
                request_id=request_id,
                idempotency_key=idempotency_key,
                actor_user_id=actor_user_id,
            )
        except ValueError as exc:
            raise _window_invalid(str(exc)) from exc
        refresh_run_id = uuid5(
            NAMESPACE_URL,
            f"long-invest:qfq-refresh:{security.security_id}:{idempotency_key}",
        )
        frozen_config = {
            "refresh_run_id": str(refresh_run_id),
            "security_id": str(command.security_id),
            "symbol": command.symbol,
            "security_master_version": security.master_version,
            "listing_status": security.listing_status.value,
            "start": command.start.isoformat(),
            "end": command.end.isoformat(),
            "as_of_date": command.as_of_date.isoformat(),
            "expected_trade_dates": [
                item.isoformat() for item in command.expected_trade_dates
            ],
            "calendar_version_id": str(window.version_id),
            "calendar_version_number": window.version_number,
            "input_daily_version": command.input_daily_version,
            "daily_source": daily.source,
            "unadjusted_close": _decimal(daily.close),
            "trigger_reason": command.trigger_reason,
            "provider": "eastmoney",
        }
        submission = SubmitJob(
            job_type="QFQ_REFRESH",
            queue="qfq-refresh",
            idempotency_scope=f"qfq-refresh:{security.security_id}",
            idempotency_key=command.idempotency_key,
            request_id=command.request_id,
            config_snapshot=frozen_config,
            business_object_type="security",
            business_object_id=str(security.security_id),
            created_by_user_id=command.actor_user_id,
            soft_timeout_seconds=240,
            hard_timeout_seconds=300,
        )
        request_hash = _refresh_request_hash(frozen_config)
        try:
            async with self._database.transaction() as session:
                repository = self._repository_factory(session)
                jobs = self._job_service_factory(session)
                await repository.lock_request(security.security_id, request_hash)
                existing_run = await repository.find_run_by_request_hash(
                    security.security_id, request_hash
                )
                if existing_run is not None:
                    existing_job = await jobs.get(existing_run.job_id)
                    if existing_job is None:
                        raise AppError(
                            code="QFQ_REFRESH_CONFLICT",
                            message="已有刷新运行记录缺少关联任务",
                            status_code=409,
                            details={"refresh_run_id": str(existing_run.id)},
                        )
                    return existing_job
                job = await jobs.submit(submission)
                run, _created = await repository.claim_run(
                    QfqRefreshRun(
                        id=refresh_run_id,
                        job_id=job.id,
                        security_id=command.security_id,
                        symbol=command.symbol,
                        requested_start=command.start,
                        requested_end=command.end,
                        as_of_date=command.as_of_date,
                        expected_trade_dates=[
                            item.isoformat() for item in command.expected_trade_dates
                        ],
                        input_daily_version=command.input_daily_version,
                        trigger_reason=command.trigger_reason,
                        request_id=command.request_id,
                        idempotency_key=command.idempotency_key,
                        request_hash=request_hash,
                        status=QfqRefreshStatus.PENDING,
                        provider="eastmoney",
                    )
                )
                if run.job_id != job.id or run.id != refresh_run_id:
                    raise AppError(
                        code="QFQ_REFRESH_CONFLICT",
                        message="刷新运行记录与任务不一致",
                        status_code=409,
                    )
                audit = AuditWrite(
                    action_code="qfq.refresh_requested",
                    object_type="security",
                    object_id=str(security.security_id),
                    result="SUCCESS",
                    request_id=request_id,
                    idempotency_key=_audit_key(
                        security.security_id, command.idempotency_key
                    ),
                    risk_level="HIGH",
                    reason=command.trigger_reason,
                    before_summary=None,
                    after_summary={
                        "job_id": str(job.id),
                        "refresh_run_id": str(run.id),
                        "start": command.start.isoformat(),
                        "end": command.end.isoformat(),
                        "as_of_date": command.as_of_date.isoformat(),
                        "calendar_version_id": str(window.version_id),
                        "input_daily_version": command.input_daily_version,
                    },
                    actor_user_id=actor_user_id,
                    session_id=session_id,
                    trusted_ip=trusted_ip,
                )
                await self._append_audit(session, audit)
                return job
        except AppError as exc:
            if exc.code == "IDEMPOTENCY_KEY_REUSED":
                raise AppError(
                    code="QFQ_REFRESH_CONFLICT",
                    message="幂等键已用于不同的前复权刷新内容",
                    status_code=409,
                    details=exc.details,
                ) from exc
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def begin_fetch(self, run_id: UUID, *, now: datetime) -> QfqRefreshView:
        return await self._advance_run(
            run_id,
            expected=QfqRefreshStatus.PENDING,
            target=QfqRefreshStatus.FETCHING,
            now=now,
        )

    async def begin_validation(self, run_id: UUID, *, now: datetime) -> QfqRefreshView:
        return await self._advance_run(
            run_id,
            expected=QfqRefreshStatus.FETCHING,
            target=QfqRefreshStatus.VALIDATING,
            now=now,
        )

    async def activate(
        self,
        run_id: UUID,
        validated_window: ValidatedQfqWindow,
        *,
        current_input_daily_version: int,
        provider_contract_version: str,
        now: datetime,
    ) -> Any:
        try:
            async with self._database.transaction() as session:
                service = self._transaction_service(session)
                return await service.activate(
                    run_id,
                    validated_window,
                    current_input_daily_version=current_input_daily_version,
                    provider_contract_version=provider_contract_version,
                    now=now,
                )
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def fail(
        self,
        run_id: UUID,
        *,
        code: str,
        retryable: bool,
        now: datetime,
    ) -> Any:
        try:
            async with self._database.transaction() as session:
                service = self._transaction_service(session)
                return await service.fail(
                    run_id,
                    code=code,
                    retryable=retryable,
                    now=now,
                )
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def _calendar_window(self, start: date, end: date):
        try:
            window = await self._calendar.trading_dates(start, end)
        except AppError as exc:
            if exc.status_code >= 500:
                raise _backend_unavailable() from exc
            raise _window_invalid("交易日历窗口不可用") from exc
        if not window.dates or window.dates[-1] != end:
            raise _window_invalid("交易日历窗口为空或不包含目标日")
        return window

    async def _call_public(self, awaitable):
        try:
            return await awaitable
        except AppError as exc:
            if exc.status_code >= 500:
                raise _backend_unavailable() from exc
            raise

    async def _append_audit(self, session: Any, audit: AuditWrite) -> None:
        service = self._audit_service_factory(session)
        try:
            async with session.begin_nested():
                await service.append(audit)
        except IntegrityError:
            existing = await service.find_by_idempotency(audit.idempotency_key)
            if existing is None or not _same_audit(existing, audit):
                raise AppError(
                    code="QFQ_REFRESH_CONFLICT",
                    message="前复权刷新审计幂等内容冲突",
                    status_code=409,
                ) from None

    def _transaction_service(self, session: Any):
        repository = self._repository_factory(session)
        events = self._event_factory(session)
        return self._domain_service_factory(repository, events=events)

    async def _advance_run(
        self,
        run_id: UUID,
        *,
        expected: QfqRefreshStatus,
        target: QfqRefreshStatus,
        now: datetime,
    ) -> QfqRefreshView:
        try:
            async with self._database.transaction() as session:
                repository = self._repository_factory(session)
                run = await repository.get_run(run_id, for_update=True)
                if run is None:
                    raise AppError(
                        code="QFQ_REFRESH_NOT_FOUND",
                        message="前复权刷新记录不存在",
                        status_code=404,
                    )
                current = QfqRefreshStatus(str(run.status))
                if _is_replay_state(current, target):
                    return _refresh_view(run)
                if current is not expected:
                    raise AppError(
                        code="QFQ_REFRESH_CONFLICT",
                        message="前复权刷新状态不能按该顺序推进",
                        status_code=409,
                    )
                run = await repository.transition_run(
                    run_id,
                    expected_status=expected,
                    status=target,
                    updated_at=now,
                )
                return _refresh_view(run)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc


def get_qfq_application() -> QfqApplication:
    database = get_database()
    return QfqApplication(
        database,
        security_application=get_security_application(),
        calendar_application=get_calendar_application(),
        daily_application=get_daily_data_application(),
    )


def _dataset_view(item: Any) -> QfqDatasetView:
    return QfqDatasetView(
        id=item.id,
        security_id=item.security_id,
        symbol=item.symbol,
        version=item.version,
        requested_start=item.requested_start,
        requested_end=item.requested_end,
        actual_start=item.actual_start,
        actual_end=item.actual_end,
        as_of_date=item.as_of_date,
        provider=item.provider,
        provider_contract_version=item.provider_contract_version,
        anchor_date=item.anchor_date,
        anchor_close=_decimal(item.anchor_close),
        row_count=item.row_count,
        checksum=item.checksum,
        lifecycle=QfqDatasetLifecycle(str(item.lifecycle)),
        freshness=QfqFreshness(str(item.freshness)),
        stale_reason=item.stale_reason,
        created_at=item.created_at,
        activated_at=item.activated_at,
        superseded_at=item.superseded_at,
    )


def _bar_view(item: Any) -> QfqBarView:
    return QfqBarView(
        trade_date=item.trade_date,
        open=_decimal(item.open),
        high=_decimal(item.high),
        low=_decimal(item.low),
        close=_decimal(item.close),
        volume=item.volume,
        amount=_decimal(item.amount),
    )


def _refresh_view(item: Any) -> QfqRefreshView:
    return QfqRefreshView(
        id=item.id,
        job_id=item.job_id,
        security_id=item.security_id,
        symbol=item.symbol,
        start=item.requested_start,
        end=item.requested_end,
        as_of_date=item.as_of_date,
        input_daily_version=item.input_daily_version,
        status=QfqRefreshStatus(str(item.status)),
        candidate_dataset_id=item.candidate_dataset_id,
        activated_dataset_id=item.activated_dataset_id,
        row_count=item.row_count,
        checksum=item.checksum,
        error_code=item.error_code,
        retryable=item.retryable,
        created_at=item.created_at,
        updated_at=item.updated_at,
        completed_at=item.completed_at,
    )


_ACTIVE_STATE_ORDER = {
    QfqRefreshStatus.PENDING: 0,
    QfqRefreshStatus.FETCHING: 1,
    QfqRefreshStatus.VALIDATING: 2,
    QfqRefreshStatus.COMMITTING: 3,
}
_TERMINAL_STATES = {
    QfqRefreshStatus.SUCCEEDED,
    QfqRefreshStatus.FAILED,
    QfqRefreshStatus.TIMED_OUT,
    QfqRefreshStatus.SUPERSEDED,
}


def _is_replay_state(current: QfqRefreshStatus, target: QfqRefreshStatus) -> bool:
    if current in _TERMINAL_STATES:
        return True
    return _ACTIVE_STATE_ORDER[current] >= _ACTIVE_STATE_ORDER[target]


def _decimal(value: Decimal) -> str:
    return format(value, "f")


def _audit_key(security_id: UUID, idempotency_key: str) -> str:
    raw = f"{security_id}\0{idempotency_key}".encode()
    return f"qfq-refresh:{hashlib.sha256(raw).hexdigest()}"


def _refresh_request_hash(frozen_config: dict[str, Any]) -> str:
    content = {
        key: value for key, value in frozen_config.items() if key != "refresh_run_id"
    }
    serialized = json.dumps(
        content,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def _same_audit(existing: Any, candidate: AuditWrite) -> bool:
    fields = (
        "action_code",
        "object_type",
        "object_id",
        "result",
        "idempotency_key",
        "risk_level",
        "reason",
        "before_summary",
        "after_summary",
        "actor_user_id",
    )
    return all(
        getattr(existing, field) == getattr(candidate, field) for field in fields
    )


def _window_invalid(message: str) -> AppError:
    return AppError(code="QFQ_WINDOW_INVALID", message=message, status_code=422)


def _backend_unavailable() -> AppError:
    return AppError(
        code="QFQ_BACKEND_UNAVAILABLE",
        message="前复权服务暂时不可用",
        status_code=503,
    )
