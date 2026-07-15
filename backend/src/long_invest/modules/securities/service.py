import hashlib
import json
from dataclasses import asdict
from datetime import date
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.securities.contracts import (
    ListingStatus,
    Market,
    SecurityEligibility,
    SecurityMasterItem,
    SecurityMasterSnapshot,
    SecurityType,
    SnapshotResult,
    UniverseQuery,
    assess_monitoring_eligibility,
    validate_symbol,
)
from long_invest.modules.securities.models import (
    Security,
    SecurityMasterVersion,
    SecurityRevision,
    SecurityUniverseSnapshot,
    SecurityUniverseSnapshotItem,
)
from long_invest.modules.securities.repository import SecurityRepository
from long_invest.platform.errors import AppError
from long_invest.platform.outbox.models import EventOutbox, OutboxStatus

_MUTABLE_FIELDS = (
    "exchange_code",
    "name",
    "market",
    "security_type",
    "listed_on",
    "delisted_on",
    "listing_status",
    "is_st",
    "is_suspended",
    "provider_codes",
)
_REQUIRED_PROVIDERS = {"eastmoney", "sina"}


class SecurityMasterService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        repository: SecurityRepository | None = None,
    ) -> None:
        self._session = session
        self._repository = repository or SecurityRepository(session)

    async def apply_snapshot(
        self, snapshot: SecurityMasterSnapshot
    ) -> SnapshotResult:
        _validate_snapshot(snapshot)
        content_hash = _snapshot_hash(snapshot)
        replay = await self._find_replay(snapshot, content_hash)
        if replay is not None:
            return replay

        version_record = SecurityMasterVersion(
            source=snapshot.source,
            source_version=snapshot.source_version,
            idempotency_key=snapshot.idempotency_key,
            content_hash=content_hash,
            master_version=await self._repository.current_master_version() + 1,
            item_count=len(snapshot.items),
        )
        claim = getattr(self._repository, "claim_master_import", None)
        if claim is None:
            self._repository.add_master_import(version_record)
            await self._repository.flush()
            claimed, created = version_record, True
        else:
            claimed, created = await claim(version_record)
        if not created:
            return _resolve_existing(claimed, content_hash)

        existing = await self._repository.get_many(
            incoming.symbol for incoming in snapshot.items
        )
        created_count = 0
        updated_count = 0
        unchanged_count = 0
        revision_count = 0
        master_version = version_record.master_version

        for incoming in snapshot.items:
            current = existing.get(incoming.symbol)
            if current is None:
                self._repository.add_security(
                    _new_security(incoming, snapshot, master_version)
                )
                created_count += 1
                continue

            before = _security_data(current)
            after = _item_data(incoming)
            changed_fields = [
                field for field in _MUTABLE_FIELDS if before[field] != after[field]
            ]
            if not changed_fields:
                unchanged_count += 1
                continue
            revision = SecurityRevision(
                security_id=current.id,
                revision_no=await self._repository.next_revision_no(current.id),
                master_version=master_version,
                changed_fields=changed_fields,
                before_data=_json_safe(before),
                after_data=_json_safe(after),
            )
            self._repository.add_revision(revision)
            _apply_item(current, incoming, snapshot, master_version)
            updated_count += 1
            revision_count += 1

        result = SnapshotResult(
            master_version=master_version,
            total_count=len(snapshot.items),
            created_count=created_count,
            updated_count=updated_count,
            unchanged_count=unchanged_count,
            revision_count=revision_count,
        )
        version_record.result_summary = asdict(result)
        self._session.add(_updated_event(snapshot, result))
        await self._repository.flush()
        return result

    async def validate_monitoring_eligibility(
        self, symbol: str
    ) -> SecurityEligibility:
        try:
            validate_symbol(symbol)
        except ValueError as exc:
            raise AppError(
                code="SECURITY_SYMBOL_INVALID",
                message=str(exc),
                status_code=422,
            ) from exc
        security = await self._repository.get_by_symbol(symbol)
        if security is None:
            raise AppError(
                code="SECURITY_NOT_FOUND",
                message="股票不存在",
                status_code=404,
            )
        return assess_monitoring_eligibility(_security_item(security))

    async def freeze_universe(
        self, query: UniverseQuery
    ) -> SecurityUniverseSnapshot:
        securities = await self._repository.list_for_universe(query)
        master_version = await self._repository.current_master_version()
        filters = {
            "markets": [item.value for item in query.markets],
            "security_types": [item.value for item in query.security_types],
            "listing_statuses": [item.value for item in query.listing_statuses],
            "include_st": query.include_st,
            "include_suspended": query.include_suspended,
            "filters": dict(query.filters),
        }
        try:
            json.dumps(filters, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise AppError(
                code="SECURITY_UNIVERSE_FILTER_INVALID",
                message="股票范围筛选条件无效",
                status_code=422,
            ) from exc
        frozen = SecurityUniverseSnapshot(
            id=uuid4(),
            filters=filters,
            item_count=len(securities),
            master_version=master_version,
        )
        items = [
            SecurityUniverseSnapshotItem(
                snapshot_id=frozen.id,
                symbol=security.symbol,
                market=security.market,
                security_type=security.security_type,
                listing_status=security.listing_status,
                is_st=security.is_st,
                is_suspended=security.is_suspended,
                master_version=security.master_version,
            )
            for security in securities
        ]
        await self._repository.save_universe_snapshot(frozen, items)
        return frozen

    async def _find_replay(
        self, snapshot: SecurityMasterSnapshot, content_hash: str
    ) -> SnapshotResult | None:
        by_key = await self._repository.find_master_import(
            source=snapshot.source,
            idempotency_key=snapshot.idempotency_key,
        )
        if by_key is not None:
            return _resolve_existing(by_key, content_hash)
        by_version = await self._repository.find_master_import(
            source=snapshot.source,
            source_version=snapshot.source_version,
        )
        if by_version is not None:
            return _resolve_existing(by_version, content_hash)
        return None


def _validate_snapshot(snapshot: SecurityMasterSnapshot) -> None:
    errors: list[dict[str, Any]] = []
    if not snapshot.source.strip() or not snapshot.source_version.strip():
        errors.append({"field": "source", "reason": "来源和来源版本不能为空"})
    if not snapshot.idempotency_key.strip():
        errors.append({"field": "idempotency_key", "reason": "幂等键不能为空"})
    if not snapshot.items:
        errors.append({"field": "items", "reason": "完整快照不能为空"})
    seen_symbols: set[str] = set()
    seen_provider_codes: set[tuple[str, str]] = set()
    for index, item in enumerate(snapshot.items):
        if item.symbol in seen_symbols:
            errors.append({"index": index, "field": "symbol", "reason": "代码重复"})
        seen_symbols.add(item.symbol)
        expected_market = item.symbol.rsplit(".", maxsplit=1)[1]
        if (
            item.exchange_code != item.symbol[:6]
            or item.market.value != expected_market
        ):
            errors.append(
                {"index": index, "field": "symbol", "reason": "代码映射不一致"}
            )
        if not item.name.strip():
            errors.append({"index": index, "field": "name", "reason": "名称不能为空"})
        if set(item.provider_codes) != _REQUIRED_PROVIDERS or any(
            not key.strip() or not value.strip()
            for key, value in item.provider_codes.items()
        ):
            errors.append(
                {
                    "index": index,
                    "field": "provider_codes",
                    "reason": "Provider 映射不完整",
                }
            )
        for provider, code in item.provider_codes.items():
            marker = (provider, code)
            if marker in seen_provider_codes:
                errors.append(
                    {
                        "index": index,
                        "field": "provider_codes",
                        "reason": "Provider 代码重复",
                    }
                )
            seen_provider_codes.add(marker)
        if (
            item.listing_status in {ListingStatus.LISTED, ListingStatus.SUSPENDED}
            and item.listed_on is None
        ):
            errors.append(
                {"index": index, "field": "listed_on", "reason": "上市日期缺失"}
            )
        if item.listing_status is ListingStatus.DELISTED and item.delisted_on is None:
            errors.append(
                {"index": index, "field": "delisted_on", "reason": "退市日期缺失"}
            )
    if errors:
        raise AppError(
            code="SECURITY_SNAPSHOT_INCOMPLETE",
            message="股票主数据快照不完整",
            status_code=422,
            details={"errors": errors},
        )


def _snapshot_hash(snapshot: SecurityMasterSnapshot) -> str:
    payload = [
        _json_safe(_item_data(item)) | {"symbol": item.symbol}
        for item in snapshot.items
    ]
    serialized = json.dumps(
        sorted(payload, key=lambda value: value["symbol"]),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def _resolve_existing(
    record: SecurityMasterVersion, content_hash: str
) -> SnapshotResult:
    if record.content_hash != content_hash:
        raise AppError(
            code="IDEMPOTENCY_KEY_REUSED",
            message="同一幂等键或来源版本不能用于不同主数据内容",
            status_code=409,
            details={"master_version": record.master_version},
        )
    summary = record.result_summary or {}
    return SnapshotResult(
        master_version=record.master_version,
        total_count=int(summary.get("total_count", record.item_count)),
        created_count=int(summary.get("created_count", 0)),
        updated_count=int(summary.get("updated_count", 0)),
        unchanged_count=int(summary.get("unchanged_count", 0)),
        revision_count=int(summary.get("revision_count", 0)),
        replayed=True,
    )


def _item_data(item: SecurityMasterItem) -> dict[str, Any]:
    return {
        "exchange_code": item.exchange_code,
        "name": item.name,
        "market": item.market.value,
        "security_type": item.security_type.value,
        "listed_on": item.listed_on,
        "delisted_on": item.delisted_on,
        "listing_status": item.listing_status.value,
        "is_st": item.is_st,
        "is_suspended": item.is_suspended,
        "provider_codes": dict(item.provider_codes),
    }


def _security_data(security: Security) -> dict[str, Any]:
    return {field: getattr(security, field) for field in _MUTABLE_FIELDS}


def _new_security(
    item: SecurityMasterItem,
    snapshot: SecurityMasterSnapshot,
    master_version: int,
) -> Security:
    return Security(
        id=uuid4(),
        symbol=item.symbol,
        **_item_data(item),
        source=snapshot.source,
        source_version=snapshot.source_version,
        master_version=master_version,
    )


def _apply_item(
    security: Security,
    item: SecurityMasterItem,
    snapshot: SecurityMasterSnapshot,
    master_version: int,
) -> None:
    for field, value in _item_data(item).items():
        setattr(security, field, value)
    security.source = snapshot.source
    security.source_version = snapshot.source_version
    security.master_version = master_version


def _security_item(security: Security) -> SecurityMasterItem:
    return SecurityMasterItem(
        symbol=security.symbol,
        exchange_code=security.exchange_code,
        name=security.name,
        market=Market(security.market),
        security_type=SecurityType(security.security_type),
        listing_status=ListingStatus(security.listing_status),
        listed_on=security.listed_on,
        delisted_on=security.delisted_on,
        is_st=security.is_st,
        is_suspended=security.is_suspended,
        provider_codes=security.provider_codes,
    )


def _json_safe(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.isoformat() if isinstance(item, date) else item
        for key, item in value.items()
    }


def _updated_event(
    snapshot: SecurityMasterSnapshot, result: SnapshotResult
) -> EventOutbox:
    dedupe_digest = hashlib.sha256(
        f"{snapshot.source}\0{snapshot.source_version}".encode()
    ).hexdigest()
    return EventOutbox(
        topic="security_master.updated",
        aggregate_type="security_master",
        aggregate_id=str(result.master_version),
        queue="default",
        payload={
            "master_version": result.master_version,
            "source": snapshot.source,
            "source_version": snapshot.source_version,
            "total_count": result.total_count,
            "created_count": result.created_count,
            "updated_count": result.updated_count,
        },
        dedupe_key=f"security-master:{dedupe_digest}",
        status=OutboxStatus.PENDING,
    )
