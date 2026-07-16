import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from long_invest.bootstrap.providers import build_provider_service
from long_invest.modules.auth.audit import AuditContext
from long_invest.modules.daily_data.contracts import (
    CreateDailyBatch,
    DailyBatchStatus,
    DailyMissingReason,
    DailyStageStatus,
    StageDailyBar,
)
from long_invest.modules.daily_data.outbox import DailyDataEventWriter
from long_invest.modules.daily_data.repository import DailyDataRepository
from long_invest.modules.daily_data.service import DailyDataService
from long_invest.modules.market_data.repository import QualityIssueRepository
from long_invest.modules.market_data.service import QualityIssueService
from long_invest.modules.providers.contracts import (
    DailyBarRequest,
    ProviderCapability,
)
from long_invest.modules.quotes.collection import QuoteCollectionService
from long_invest.modules.quotes.contracts import (
    CreateQuoteCycle,
    QuoteCycleStatus,
    QuoteSubmission,
)
from long_invest.modules.quotes.outbox import TransactionalQuoteEventAdapter
from long_invest.modules.quotes.repository import QuoteCycleRepository
from long_invest.modules.quotes.service import QuoteCycleService
from long_invest.modules.securities.application import SecurityApplication
from long_invest.modules.securities.contracts import (
    ListingStatus,
    Market,
    SecurityAuditContext,
    SecurityMasterItem,
    SecurityMasterSnapshot,
    SecurityType,
)
from long_invest.platform.database.engine import get_database
from long_invest.platform.jobs.contracts import (
    JobExecutionContext,
    JobItemStatus,
    JobResult,
    SubmitJob,
)
from long_invest.platform.jobs.service import JobService
from long_invest.platform.outbox.service import TransactionalOutboxWriter


class DatabaseQuoteProvider:
    def __init__(self, database: Any) -> None:
        self._database = database

    async def realtime_quotes_from(self, provider_code, symbols, deadline):
        async with self._database.transaction() as session:
            return await build_provider_service(session).realtime_quotes_from(
                provider_code, symbols, deadline
            )


class DatabaseQuoteCycles:
    def __init__(self, database: Any) -> None:
        self._database = database

    async def create_and_start(
        self, command: CreateQuoteCycle, now: datetime
    ):
        async with self._database.transaction() as session:
            service = _quote_cycle_service(session)
            cycle = await service.create(command)
            return await service.start(cycle.id, now)

    async def submit(
        self, cycle_id: UUID, submission: QuoteSubmission, now: datetime
    ) -> None:
        async with self._database.transaction() as session:
            await _quote_cycle_service(session).submit(cycle_id, submission, now)

    async def finalize(self, cycle_id: UUID, now: datetime):
        async with self._database.transaction() as session:
            return await _quote_cycle_service(session).finalize(cycle_id, now)

    async def cancel(self, cycle_id: UUID, now: datetime, reason: str):
        async with self._database.transaction() as session:
            return await _quote_cycle_service(session).cancel(cycle_id, now, reason)


def _quote_cycle_service(session: Any) -> QuoteCycleService:
    return QuoteCycleService(
        QuoteCycleRepository(session),
        events=TransactionalQuoteEventAdapter(session),
        quality_issues=QualityIssueService(QualityIssueRepository(session)),
    )


async def realtime_quote_cycle(context: JobExecutionContext) -> JobResult:
    try:
        command = _quote_command(context)
    except (KeyError, TypeError, ValueError):
        return JobResult.failure(
            code="QUOTE_CYCLE_CONFIG_INVALID",
            message="实时行情任务缺少有效的冻结范围或截止时间",
            retryable=False,
        )
    database = get_database()
    result = await QuoteCollectionService(
        DatabaseQuoteProvider(database),
        DatabaseQuoteCycles(database),
    ).collect(command)
    data = {
        "cycle_id": str(result.id),
        "status": result.status.value,
        "expected_count": result.expected_count,
        "valid_count": result.valid_count,
        "missing_count": result.missing_count,
        "conflict_count": result.conflict_count,
        "failed_count": result.failed_count,
    }
    if result.status is QuoteCycleStatus.READY:
        return JobResult.success_result(data=data, message="实时行情批次采集完成")
    if result.status is QuoteCycleStatus.PARTIAL:
        return JobResult(
            success=True,
            code="PARTIAL",
            message="实时行情批次部分完成",
            retryable=False,
            data=data,
        )
    return JobResult.failure(
        code="QUOTE_CYCLE_FAILED",
        message="实时行情批次没有可用报价",
        retryable=False,
        data=data,
    )


def _quote_command(context: JobExecutionContext) -> CreateQuoteCycle:
    config = context.config
    symbols = tuple(str(item) for item in config["symbols"])
    return CreateQuoteCycle(
        symbols=symbols,
        scheduled_at=datetime.fromisoformat(str(config["requested_at"])),
        timeout_seconds=int(config["timeout_seconds"]),
        idempotency_scope="quote-cycle:job",
        idempotency_key=str(context.job_id),
        universe_snapshot_id=str(config["universe_snapshot_id"]),
        universe_snapshot_version=int(config["universe_snapshot_version"]),
    )


async def quote_diagnostic(context: JobExecutionContext) -> JobResult:
    try:
        symbols = tuple(str(item) for item in context.config["symbols"])
        audit = context.config["audit"]
        reason = str(audit["reason"])
        audit_context = AuditContext(
            request_id=str(audit["request_id"]),
            idempotency_key=str(audit["idempotency_key"]),
            actor_user_id=str(audit["actor_user_id"]),
            session_id=str(audit["session_id"]),
            trusted_ip=str(audit["trusted_ip"]),
        )
        audit_values = (
            audit_context.request_id,
            audit_context.idempotency_key,
            audit_context.actor_user_id,
            audit_context.session_id,
            audit_context.trusted_ip,
            reason,
        )
        if not symbols or any(not value or not value.strip() for value in audit_values):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return JobResult.failure(
            code="QUOTE_DIAGNOSTIC_CONFIG_INVALID",
            message="行情诊断任务缺少完整的冻结范围或审计信息",
            retryable=False,
        )
    database = get_database()
    async with database.transaction() as session:
        result = await build_provider_service(session).quote_diagnostics(
            symbols,
            reason=reason,
            audit_context=audit_context,
        )
    return JobResult.success_result(data=result, message="行情诊断完成")


async def security_master_refresh(context: JobExecutionContext) -> JobResult:
    config = context.config
    required = (
        "source",
        "idempotency_key",
        "request_id",
        "created_by_user_id",
    )
    if any(not str(config.get(field, "")).strip() for field in required):
        return JobResult.failure(
            code="SECURITY_REFRESH_CONFIG_INVALID",
            message="股票主数据刷新任务缺少冻结上下文",
            retryable=False,
        )

    database = get_database()
    try:
        async with database.session() as session:
            records = await build_provider_service(session).security_master(
                datetime.now(UTC) + timedelta(seconds=30)
            )
    except Exception:
        return JobResult.failure(
            code="SECURITY_PROVIDER_UNAVAILABLE",
            message="股票主数据来源暂时不可用",
            retryable=True,
        )
    if not records:
        return JobResult.failure(
            code="SECURITY_MASTER_EMPTY",
            message="股票主数据来源返回空结果",
            retryable=True,
        )

    observed_at = max(record.observed_at for record in records)
    snapshot = SecurityMasterSnapshot(
        source=str(config["source"]),
        source_version=observed_at.isoformat(),
        idempotency_key=str(config["idempotency_key"]),
        items=tuple(_security_item(record) for record in records),
    )
    result = await SecurityApplication(
        database,
        outbox_writer=TransactionalOutboxWriter(),
    ).apply_snapshot(
        snapshot,
        audit_context=SecurityAuditContext(
            request_id=str(config["request_id"]),
            idempotency_key=str(config["idempotency_key"]),
            actor_user_id=str(config["created_by_user_id"]),
            session_id="maintenance-worker",
            trusted_ip="internal-worker",
            reason="scheduled security master refresh",
        ),
    )
    return JobResult.success_result(
        data={
            "master_version": result.master_version,
            "total_count": result.total_count,
            "created_count": result.created_count,
            "updated_count": result.updated_count,
            "unchanged_count": result.unchanged_count,
            "revision_count": result.revision_count,
            "replayed": result.replayed,
        },
        message="股票主数据刷新完成",
    )


def _security_item(record) -> SecurityMasterItem:
    if record.listed is False:
        status = ListingStatus.DELISTED
    elif record.suspended is True:
        status = ListingStatus.SUSPENDED
    elif record.listed is True:
        status = ListingStatus.LISTED
    else:
        status = ListingStatus.DATA_MISSING
    return SecurityMasterItem(
        symbol=record.symbol,
        exchange_code=record.symbol[:6],
        name=record.name,
        market=Market(record.market),
        security_type=SecurityType(record.security_type),
        listing_status=status,
        listed_on=record.listed_on,
        delisted_on=record.delisted_on,
        is_st=record.is_st,
        is_suspended=record.suspended is True,
        provider_codes={record.source.value: record.symbol[:6]},
    )


async def daily_data_coordinate(context: JobExecutionContext) -> JobResult:
    return await _daily_coordinate(context, parent_batch_id=None)


async def daily_data_retry(context: JobExecutionContext) -> JobResult:
    try:
        parent_batch_id = UUID(str(context.config["original_batch_id"]))
    except (KeyError, TypeError, ValueError):
        return JobResult.failure(
            code="DAILY_RETRY_CONFIG_INVALID",
            message="日线重试任务缺少原批次编号",
            retryable=False,
        )
    return await _daily_coordinate(context, parent_batch_id=parent_batch_id)


async def _daily_coordinate(
    context: JobExecutionContext, *, parent_batch_id: UUID | None
) -> JobResult:
    try:
        snapshot_id = UUID(str(context.config["universe_snapshot_id"]))
        trading_date = date.fromisoformat(str(context.config["trading_date"]))
        requested = tuple(str(item) for item in context.config.get("symbols", ()))
    except (KeyError, TypeError, ValueError):
        return JobResult.failure(
            code="DAILY_COORDINATE_CONFIG_INVALID",
            message="日线协调任务缺少有效日期或冻结范围",
            retryable=False,
        )
    database = get_database()
    frozen = await SecurityApplication(database).frozen_universe(snapshot_id)
    by_symbol = {item.symbol: item for item in frozen.items}
    symbols = requested or tuple(by_symbol)
    try:
        corporate_action_symbols = _corporate_action_scope(context.config, symbols)
    except ValueError:
        return JobResult.failure(
            code="DAILY_COORDINATE_CONFIG_INVALID",
            message="日线协调任务的公司行为上下文无效",
            retryable=False,
        )
    if (
        not symbols
        or any(symbol not in by_symbol for symbol in symbols)
        or not corporate_action_symbols.issubset(symbols)
    ):
        return JobResult.failure(
            code="DAILY_COORDINATE_SCOPE_INVALID",
            message="日线任务股票范围与冻结快照不一致",
            retryable=False,
        )
    scope = tuple(by_symbol[symbol] for symbol in symbols)
    batch_command = CreateDailyBatch(
        trading_date=trading_date,
        universe_snapshot_id=snapshot_id,
        symbols=symbols,
        security_ids=tuple(item.security_id for item in scope),
        known_corporate_action_symbols=tuple(
            symbol for symbol in symbols if symbol in corporate_action_symbols
        ),
        idempotency_key=f"daily-job:{context.job_id}",
        parent_batch_id=parent_batch_id,
    )
    async with database.transaction() as session:
        batch = await DailyDataService(DailyDataRepository(session)).create(
            batch_command
        )
        jobs = JobService(session)
        await jobs.initialize_items(context.job_id, symbols)
        for item in scope:
            await jobs.submit(
                _daily_item_job(
                    context,
                    batch.id,
                    trading_date,
                    item,
                    has_known_corporate_action=item.symbol
                    in corporate_action_symbols,
                )
            )
    return JobResult(
        success=True,
        code="CHILDREN_PENDING",
        message="日线逐股任务已经创建",
        retryable=False,
        data={"batch_id": str(batch.id), "item_count": len(scope)},
    )


def _corporate_action_scope(
    config: Any, symbols: tuple[str, ...]
) -> frozenset[str]:
    values = tuple(
        str(item) for item in config.get("known_corporate_action_symbols", ())
    )
    if len(values) != len(set(values)) or not set(values).issubset(symbols):
        raise ValueError("known corporate action symbols must be inside scope")
    return frozenset(values)


def _daily_item_job(
    context,
    batch_id,
    trading_date,
    item,
    *,
    has_known_corporate_action: bool = False,
) -> SubmitJob:
    completion_job = _daily_finalize_job(context.job_id, batch_id)
    return SubmitJob(
        job_type="DAILY_DATA_ITEM",
        queue="daily-market-data",
        idempotency_scope=f"daily-data:item:{context.job_id}",
        idempotency_key=item.symbol,
        request_id=str(context.job_id),
        config_snapshot={
            "parent_job_id": str(context.job_id),
            "batch_id": str(batch_id),
            "trading_date": trading_date.isoformat(),
            "security_id": str(item.security_id),
            "symbol": item.symbol,
            "is_suspended": item.is_suspended,
            "listed_on": item.listed_on.isoformat() if item.listed_on else None,
            "delisted_on": item.delisted_on.isoformat() if item.delisted_on else None,
            "is_st": item.is_st,
            "has_known_corporate_action": has_known_corporate_action,
            "linked_item": {
                "parent_job_id": str(context.job_id),
                "item_key": item.symbol,
                "completion_job": _job_snapshot(completion_job),
            },
        },
        business_object_type="daily_data_batch",
        business_object_id=str(batch_id),
        soft_timeout_seconds=240,
        hard_timeout_seconds=300,
    )


def _job_snapshot(command: SubmitJob) -> dict[str, object]:
    return {
        "job_type": command.job_type,
        "queue": command.queue,
        "idempotency_scope": command.idempotency_scope,
        "idempotency_key": command.idempotency_key,
        "request_id": command.request_id,
        "config_snapshot": command.config_snapshot,
        "priority": command.priority,
        "business_object_type": command.business_object_type,
        "business_object_id": command.business_object_id,
        "created_by_user_id": command.created_by_user_id,
        "soft_timeout_seconds": command.soft_timeout_seconds,
        "hard_timeout_seconds": command.hard_timeout_seconds,
    }


async def daily_data_item(context: JobExecutionContext) -> JobResult:
    try:
        config = context.config
        parent_job_id = UUID(str(config["parent_job_id"]))
        batch_id = UUID(str(config["batch_id"]))
        security_id = UUID(str(config["security_id"]))
        trading_date = date.fromisoformat(str(config["trading_date"]))
        symbol = str(config["symbol"])
        stage = _known_daily_absence(config, security_id, symbol, trading_date)
    except (KeyError, TypeError, ValueError):
        return JobResult.failure(
            code="DAILY_ITEM_CONFIG_INVALID",
            message="逐股日线任务配置无效",
            retryable=False,
        )
    if stage is None:
        stage = await _fetch_daily_stage(
            security_id,
            symbol,
            trading_date,
            is_new_listing=(
                bool(config.get("listed_on"))
                and date.fromisoformat(str(config["listed_on"])) == trading_date
            ),
            is_st=bool(config.get("is_st")),
            has_known_corporate_action=bool(
                config.get("has_known_corporate_action")
            ),
        )
    item_status = _daily_item_status(stage)
    database = get_database()
    async with database.transaction() as session:
        await DailyDataService(DailyDataRepository(session)).stage(batch_id, stage)
        jobs = JobService(session)
        completed, total, all_terminal = await jobs.finish_item(
            child_job_id=context.job_id,
            fence_token=context.fence_token,
            parent_job_id=parent_job_id,
            item_key=symbol,
            status=item_status,
            result_ref={"batch_id": str(batch_id), "stage_status": stage.status.value},
            error_code=stage.error_code,
        )
        if all_terminal:
            await jobs.submit(_daily_finalize_job(parent_job_id, batch_id))
    return JobResult.success_result(
        data={
            "batch_id": str(batch_id),
            "symbol": symbol,
            "stage_status": stage.status.value,
            "completed": completed,
            "total": total,
        },
        message="逐股日线任务完成",
    )


def _daily_finalize_job(parent_job_id: UUID, batch_id: UUID) -> SubmitJob:
    return SubmitJob(
        job_type="DAILY_DATA_FINALIZE",
        queue="daily-market-data",
        idempotency_scope=f"daily-data:finalize:{parent_job_id}",
        idempotency_key=str(batch_id),
        request_id=str(parent_job_id),
        config_snapshot={
            "parent_job_id": str(parent_job_id),
            "linked_parent_job_id": str(parent_job_id),
            "batch_id": str(batch_id),
        },
        business_object_type="daily_data_batch",
        business_object_id=str(batch_id),
        soft_timeout_seconds=300,
        hard_timeout_seconds=600,
    )


async def _fetch_daily_stage(
    security_id: UUID,
    symbol: str,
    trading_date: date,
    *,
    is_new_listing: bool,
    is_st: bool,
    has_known_corporate_action: bool,
) -> StageDailyBar:
    now = datetime.now(UTC)
    database = get_database()
    try:
        async with asyncio.timeout(200):
            async with database.transaction() as session:
                result = await build_provider_service(session).daily_bars(
                    DailyBarRequest(
                        symbol=symbol,
                        start=trading_date,
                        end=trading_date,
                        capability=ProviderCapability.DAILY_BAR_UNADJUSTED,
                    ),
                    now + timedelta(seconds=180),
                )
    except Exception as exc:
        return _failed_daily_stage(
            security_id,
            symbol,
            trading_date,
            str(getattr(exc, "code", "DAILY_PROVIDER_FAILED")),
        )
    bar = next(
        (
            item
            for item in result.items
            if item.symbol == symbol and item.trading_date == trading_date
        ),
        None,
    )
    if bar is None:
        failure = next(
            (item for item in result.failures if item.symbol == symbol), None
        )
        return _failed_daily_stage(
            security_id,
            symbol,
            trading_date,
            failure.code
            if failure
            else result.batch_error_code or "DAILY_BAR_MISSING",
        )
    return StageDailyBar(
        symbol=symbol,
        security_id=security_id,
        trading_date=trading_date,
        status=DailyStageStatus.FETCHED,
        received_at=datetime.now(UTC),
        provider_payload={
            "symbol": bar.symbol,
            "trading_date": bar.trading_date,
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": bar.volume,
            "amount": str(bar.amount),
            "source": bar.source.value,
            "is_new_listing": is_new_listing,
            "is_st": is_st,
            "has_known_corporate_action": has_known_corporate_action,
        },
    )


def _failed_daily_stage(
    security_id: UUID, symbol: str, trading_date: date, error_code: str
) -> StageDailyBar:
    return StageDailyBar(
        symbol=symbol,
        security_id=security_id,
        trading_date=trading_date,
        status=DailyStageStatus.FAILED,
        received_at=datetime.now(UTC),
        error_code=error_code,
    )


def _known_daily_absence(
    config: Any, security_id: UUID, symbol: str, trading_date: date
) -> StageDailyBar | None:
    listed_on = (
        date.fromisoformat(str(config["listed_on"]))
        if config.get("listed_on")
        else None
    )
    delisted_on = (
        date.fromisoformat(str(config["delisted_on"]))
        if config.get("delisted_on")
        else None
    )
    reason = None
    if listed_on and trading_date < listed_on:
        reason = DailyMissingReason.NOT_YET_LISTED
    elif delisted_on and trading_date > delisted_on:
        reason = DailyMissingReason.DELISTED
    elif bool(config.get("is_suspended")):
        reason = DailyMissingReason.SUSPENDED
    if reason is None:
        return None
    return StageDailyBar(
        symbol=symbol,
        security_id=security_id,
        trading_date=trading_date,
        status=DailyStageStatus.MISSING,
        received_at=datetime.now(UTC),
        missing_reason=reason,
        error_code=f"DAILY_{reason.value}",
    )


def _daily_item_status(stage: StageDailyBar) -> JobItemStatus:
    if stage.status is DailyStageStatus.FETCHED:
        return JobItemStatus.SUCCEEDED
    if (
        stage.status is DailyStageStatus.MISSING
        and stage.missing_reason is not DailyMissingReason.UNEXPLAINED
    ):
        return JobItemStatus.SKIPPED
    return JobItemStatus.FAILED


async def daily_data_finalize(context: JobExecutionContext) -> JobResult:
    try:
        batch_id = UUID(str(context.config["batch_id"]))
        parent_job_id = UUID(str(context.config["parent_job_id"]))
    except (KeyError, TypeError, ValueError):
        return JobResult.failure(
            code="DAILY_FINALIZE_CONFIG_INVALID",
            message="日线汇总任务缺少批次编号",
            retryable=False,
        )
    database = get_database()
    async with database.transaction() as session:
        service = DailyDataService(
            DailyDataRepository(session),
            events=DailyDataEventWriter(session),
            quality_issues=QualityIssueService(QualityIssueRepository(session)),
        )
        await service.validate(batch_id)
        result = await service.commit(batch_id)
        job_result = _daily_batch_result(result)
        await JobService(session).finalize_parent(parent_job_id, job_result)
    return job_result


def _daily_batch_result(result: Any) -> JobResult:
    data = {
        "batch_id": str(result.id),
        "status": result.status.value,
        "expected_count": result.expected_count,
        "committed_count": result.committed_count,
        "missing_count": result.missing_count,
        "failed_count": result.failed_count,
    }
    if result.status is DailyBatchStatus.SUCCEEDED:
        return JobResult.success_result(data=data, message="日线批次提交完成")
    if result.status is DailyBatchStatus.PARTIAL:
        return JobResult(
            success=True,
            code="PARTIAL",
            message="日线批次部分完成",
            retryable=False,
            data=data,
        )
    return JobResult.failure(
        code="DAILY_BATCH_FAILED",
        message="日线批次没有可提交数据",
        retryable=False,
        data=data,
    )
