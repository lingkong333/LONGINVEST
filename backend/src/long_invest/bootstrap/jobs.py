from datetime import UTC, datetime, timedelta

from long_invest.bootstrap.providers import build_provider_service
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
from long_invest.platform.jobs.contracts import JobResult
from long_invest.platform.outbox.service import TransactionalOutboxWriter


async def security_master_refresh(config: dict) -> JobResult:
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
