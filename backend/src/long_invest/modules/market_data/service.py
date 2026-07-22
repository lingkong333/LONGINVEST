from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid5

from long_invest.modules.market_data.contracts import (
    AdjustmentTimelineEntry,
    AdjustmentTimelineSnapshot,
    OpenQualityIssue,
    QualityIssuePage,
    QualityIssueStatus,
    QualityIssueView,
    QualityResolutionAction,
    QualitySeverity,
    RequestQualityRefetch,
    ResolveQualityIssue,
)
from long_invest.modules.market_data.integrations import QualityEventPort
from long_invest.modules.market_data.models import (
    CorporateActionFact,
    CorporateActionFetchBatch,
    DataQualityIssue,
)
from long_invest.platform.errors import AppError

TERMINAL_STATUSES = frozenset(
    {QualityIssueStatus.RESOLVED, QualityIssueStatus.INVALIDATED}
)
SEVERITY_RANK = {
    QualitySeverity.INFO: 0,
    QualitySeverity.WARNING: 1,
    QualitySeverity.ERROR: 2,
    QualitySeverity.CRITICAL: 3,
}


@dataclass(frozen=True, slots=True)
class CorporateActionFactInput:
    source_event_id: str
    event_type: str
    event_date: date
    effective_date: date
    published_at: datetime
    observed_at: datetime
    adjustment_factor: Decimal
    source_reference: str
    raw_content_hash: str


@dataclass(frozen=True, slots=True)
class RecordCorporateActionFetch:
    batch_id: UUID
    security_id: UUID
    source: str
    provider_contract_version: str
    coverage_start: date
    coverage_end: date
    observed_at: datetime
    fetched_at: datetime
    succeeded: bool
    facts: tuple[CorporateActionFactInput, ...] = ()
    error_code: str | None = None


class CorporateActionRepositoryPort(Protocol):
    async def get_batch(self, batch_id: UUID) -> CorporateActionFetchBatch | None: ...

    async def list_event_facts_for_update(
        self,
        *,
        security_id: UUID,
        source: str,
        source_event_ids: tuple[str, ...],
    ) -> list[CorporateActionFact]: ...

    async def claim_fetch(
        self,
        batch: CorporateActionFetchBatch,
        facts: tuple[CorporateActionFact, ...],
    ) -> tuple[CorporateActionFetchBatch | None, bool]: ...

    async def list_covering_batches(
        self,
        *,
        security_id: UUID,
        start_date: date,
        end_date: date,
        as_of: datetime,
    ) -> list[CorporateActionFetchBatch]: ...

    async def list_facts(
        self,
        *,
        security_id: UUID,
        source: str,
        start_date: date,
        end_date: date,
        as_of: datetime,
        observed_through: datetime,
    ) -> list[CorporateActionFact]: ...


class CorporateActionService:
    def __init__(self, repository: CorporateActionRepositoryPort) -> None:
        self._repository = repository

    async def record_fetch(self, command: RecordCorporateActionFetch) -> UUID:
        digest = _fetch_content_hash(command)
        existing = await self._repository.get_batch(command.batch_id)
        if existing is not None:
            if existing.content_hash != digest:
                raise _adjustment_unavailable("公司行动抓取批次编号已用于其他内容")
            return existing.id

        _validate_fetch(command)
        _reject_duplicate_source_events(command.facts)
        event_ids = tuple(fact.source_event_id for fact in command.facts)
        for _attempt in range(3):
            history = await self._repository.list_event_facts_for_update(
                security_id=command.security_id,
                source=command.source,
                source_event_ids=event_ids,
            )
            facts = _new_fact_revisions(command, history)
            claimed, created = await self._repository.claim_fetch(
                _new_fetch_batch(command, digest), facts
            )
            if claimed is None:
                continue
            if not created and claimed.content_hash != digest:
                raise _adjustment_unavailable("公司行动抓取批次编号已用于其他内容")
            return claimed.id
        raise _adjustment_unavailable("公司行动修订并发写入冲突")

    async def get_adjustment_timeline(
        self,
        *,
        security_id: UUID,
        start_date: date,
        end_date: date,
        as_of: datetime,
    ) -> AdjustmentTimelineSnapshot:
        if start_date > end_date or not _is_aware(as_of):
            raise _adjustment_unavailable("公司行动查询范围或冻结时点无效")
        batches = await self._repository.list_covering_batches(
            security_id=security_id,
            start_date=start_date,
            end_date=end_date,
            as_of=as_of,
        )
        batch = _select_covering_batch(batches)
        facts = await self._repository.list_facts(
            security_id=security_id,
            source=batch.source,
            start_date=start_date,
            end_date=end_date,
            as_of=as_of,
            observed_through=batch.observed_at,
        )
        selected = _select_point_in_time_facts(facts, batch=batch, as_of=as_of)
        entries = _merge_effective_dates(selected, batch.source)
        content_hash = _timeline_hash(
            security_id=security_id,
            start_date=start_date,
            end_date=end_date,
            as_of=as_of,
            source=batch.source,
            provider_contract_version=batch.provider_contract_version,
            fetched_at=batch.fetched_at,
            entries=entries,
        )
        return AdjustmentTimelineSnapshot(
            snapshot_id=uuid5(batch.id, content_hash),
            security_id=security_id,
            start_date=start_date,
            end_date=end_date,
            as_of=as_of,
            source=batch.source,
            provider_contract_version=batch.provider_contract_version,
            fetched_at=batch.fetched_at,
            row_count=len(entries),
            content_hash=content_hash,
            entries=entries,
        )


def _validate_fetch(command: RecordCorporateActionFetch) -> None:
    if command.coverage_start > command.coverage_end:
        raise _adjustment_unavailable("公司行动抓取覆盖范围无效")
    if not _is_aware(command.observed_at) or not _is_aware(command.fetched_at):
        raise _adjustment_unavailable("公司行动抓取时间必须包含时区")
    if command.observed_at > command.fetched_at:
        raise _adjustment_unavailable("公司行动观察时间晚于抓取完成时间")
    if not command.source.strip() or not command.provider_contract_version.strip():
        raise _adjustment_unavailable("公司行动来源信息不完整")
    if command.succeeded:
        if command.error_code is not None:
            raise _adjustment_unavailable("成功批次不能包含失败原因")
    elif command.error_code is None or not command.error_code.strip():
        raise _adjustment_unavailable("失败批次缺少失败原因")
    elif command.facts:
        raise _adjustment_unavailable("失败批次不能保存公司行动事实")

    for fact in command.facts:
        if (
            not fact.source_event_id.strip()
            or not fact.event_type.strip()
            or not fact.source_reference.strip()
        ):
            raise _adjustment_unavailable("公司行动关键来源字段不完整")
        if not _is_aware(fact.published_at) or not _is_aware(fact.observed_at):
            raise _adjustment_unavailable("公司行动时间必须包含时区")
        if fact.published_at > fact.observed_at:
            raise _adjustment_unavailable("公司行动早于公告时间被观察")
        if fact.observed_at > command.observed_at:
            raise _adjustment_unavailable("公司行动事实晚于批次冻结时点")
        if fact.event_date > fact.effective_date:
            raise _adjustment_unavailable("公司行动事件日期晚于生效日期")
        if not command.coverage_start <= fact.effective_date <= command.coverage_end:
            raise _adjustment_unavailable("公司行动事实超出批次覆盖范围")
        if not fact.adjustment_factor.is_finite() or fact.adjustment_factor <= 0:
            raise _adjustment_unavailable("公司行动调整因子无效")
        if not _is_sha256(fact.raw_content_hash):
            raise _adjustment_unavailable("公司行动原始内容哈希无效")


def _reject_duplicate_source_events(
    facts: tuple[CorporateActionFactInput, ...],
) -> None:
    source_event_ids = [fact.source_event_id for fact in facts]
    if len(source_event_ids) != len(set(source_event_ids)):
        raise _adjustment_unavailable("同一抓取批次包含重复的来源事件")


def _new_fetch_batch(
    command: RecordCorporateActionFetch, content_hash: str
) -> CorporateActionFetchBatch:
    return CorporateActionFetchBatch(
        id=command.batch_id,
        security_id=command.security_id,
        source=command.source,
        provider_contract_version=command.provider_contract_version,
        coverage_start=command.coverage_start,
        coverage_end=command.coverage_end,
        observed_at=command.observed_at,
        fetched_at=command.fetched_at,
        status="SUCCESS" if command.succeeded else "FAILED",
        row_count=len(command.facts),
        content_hash=content_hash,
        error_code=command.error_code,
    )


def _new_fact_revisions(
    command: RecordCorporateActionFetch,
    history: list[CorporateActionFact],
) -> tuple[CorporateActionFact, ...]:
    by_event: dict[str, list[CorporateActionFact]] = {}
    for stored in history:
        by_event.setdefault(stored.source_event_id, []).append(stored)

    revisions: list[CorporateActionFact] = []
    for incoming in command.facts:
        previous = by_event.get(incoming.source_event_id, [])
        same_content = next(
            (
                stored
                for stored in previous
                if stored.raw_content_hash == incoming.raw_content_hash
            ),
            None,
        )
        if same_content is not None:
            if _stored_matches_input(same_content, incoming):
                continue
            raise _adjustment_unavailable("相同公司行动原始内容产生了冲突字段")
        revision_no = max((stored.revision_no for stored in previous), default=0) + 1
        revisions.append(
            CorporateActionFact(
                batch_id=command.batch_id,
                security_id=command.security_id,
                source=command.source,
                source_event_id=incoming.source_event_id,
                event_type=incoming.event_type,
                event_date=incoming.event_date,
                effective_date=incoming.effective_date,
                published_at=incoming.published_at,
                observed_at=incoming.observed_at,
                revision_no=revision_no,
                adjustment_factor=incoming.adjustment_factor,
                source_reference=incoming.source_reference,
                raw_content_hash=incoming.raw_content_hash,
            )
        )
    return tuple(revisions)


def _stored_matches_input(
    stored: CorporateActionFact, incoming: CorporateActionFactInput
) -> bool:
    return (
        stored.event_type == incoming.event_type
        and stored.event_date == incoming.event_date
        and stored.effective_date == incoming.effective_date
        and stored.published_at == incoming.published_at
        and stored.adjustment_factor == incoming.adjustment_factor
        and stored.source_reference == incoming.source_reference
    )


def _select_covering_batch(
    batches: list[CorporateActionFetchBatch],
) -> CorporateActionFetchBatch:
    if not batches:
        raise _adjustment_unavailable("没有覆盖查询范围的公司行动成功批次")
    selected = batches[0]
    peers = [
        batch
        for batch in batches
        if batch.observed_at == selected.observed_at
        and batch.fetched_at == selected.fetched_at
    ]
    if any(batch.content_hash != selected.content_hash for batch in peers[1:]):
        raise _adjustment_unavailable("最新公司行动覆盖批次存在冲突")
    return selected


def _select_point_in_time_facts(
    facts: list[CorporateActionFact],
    *,
    batch: CorporateActionFetchBatch,
    as_of: datetime,
) -> tuple[CorporateActionFact, ...]:
    eligible = [
        fact
        for fact in facts
        if fact.observed_at <= as_of
        and fact.observed_at <= batch.observed_at
        and fact.published_at <= as_of
        and fact.published_at.date() <= fact.effective_date
    ]
    grouped: dict[str, list[CorporateActionFact]] = {}
    for fact in eligible:
        grouped.setdefault(fact.source_event_id, []).append(fact)

    selected: list[CorporateActionFact] = []
    for source_event_id in sorted(grouped):
        revisions = grouped[source_event_id]
        revision_no = max(fact.revision_no for fact in revisions)
        latest = [fact for fact in revisions if fact.revision_no == revision_no]
        signatures = {_fact_signature(fact) for fact in latest}
        if len(signatures) != 1:
            raise _adjustment_unavailable("同一来源的公司行动修订内容冲突")
        selected.append(min(latest, key=lambda fact: str(fact.id)))
    return tuple(selected)


def _merge_effective_dates(
    facts: tuple[CorporateActionFact, ...], source: str
) -> tuple[AdjustmentTimelineEntry, ...]:
    grouped: dict[date, list[CorporateActionFact]] = {}
    for fact in facts:
        grouped.setdefault(fact.effective_date, []).append(fact)

    entries: list[AdjustmentTimelineEntry] = []
    for effective_date in sorted(grouped):
        day_facts = sorted(
            grouped[effective_date],
            key=lambda fact: (fact.source_event_id, fact.revision_no, str(fact.id)),
        )
        factor = Decimal(1)
        for fact in day_facts:
            factor *= fact.adjustment_factor
        entries.append(
            AdjustmentTimelineEntry(
                event_date=min(fact.event_date for fact in day_facts),
                effective_date=effective_date,
                published_at=max(fact.published_at for fact in day_facts),
                source=source,
                adjustment_factor=factor,
                data_hash=_facts_hash(day_facts),
            )
        )
    return tuple(entries)


def _fetch_content_hash(command: RecordCorporateActionFetch) -> str:
    payload = {
        "security_id": str(command.security_id),
        "source": command.source,
        "provider_contract_version": command.provider_contract_version,
        "coverage_start": command.coverage_start.isoformat(),
        "coverage_end": command.coverage_end.isoformat(),
        "observed_at": _datetime_text(command.observed_at),
        "fetched_at": _datetime_text(command.fetched_at),
        "succeeded": command.succeeded,
        "error_code": command.error_code,
        "facts": sorted(
            (_input_fact_payload(fact) for fact in command.facts),
            key=lambda item: (
                str(item["source_event_id"]),
                str(item["raw_content_hash"]),
            ),
        ),
    }
    return _json_hash(payload)


def _facts_hash(facts: list[CorporateActionFact]) -> str:
    return _json_hash([_stored_fact_payload(fact) for fact in facts])


def _timeline_hash(
    *,
    security_id: UUID,
    start_date: date,
    end_date: date,
    as_of: datetime,
    source: str,
    provider_contract_version: str,
    fetched_at: datetime,
    entries: tuple[AdjustmentTimelineEntry, ...],
) -> str:
    return _json_hash(
        {
            "security_id": str(security_id),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "as_of": _datetime_text(as_of),
            "source": source,
            "provider_contract_version": provider_contract_version,
            "fetched_at": _datetime_text(fetched_at),
            "entries": [
                {
                    "event_date": entry.event_date.isoformat(),
                    "effective_date": entry.effective_date.isoformat(),
                    "published_at": _datetime_text(entry.published_at),
                    "adjustment_factor": _decimal_text(entry.adjustment_factor),
                    "data_hash": entry.data_hash,
                }
                for entry in entries
            ],
        }
    )


def _input_fact_payload(fact: CorporateActionFactInput) -> dict[str, object]:
    return {
        "source_event_id": fact.source_event_id,
        "event_type": fact.event_type,
        "event_date": fact.event_date.isoformat(),
        "effective_date": fact.effective_date.isoformat(),
        "published_at": _datetime_text(fact.published_at),
        "observed_at": _datetime_text(fact.observed_at),
        "adjustment_factor": _decimal_text(fact.adjustment_factor),
        "source_reference": fact.source_reference,
        "raw_content_hash": fact.raw_content_hash,
    }


def _stored_fact_payload(fact: CorporateActionFact) -> dict[str, object]:
    return {
        "source_event_id": fact.source_event_id,
        "event_type": fact.event_type,
        "event_date": fact.event_date.isoformat(),
        "effective_date": fact.effective_date.isoformat(),
        "published_at": _datetime_text(fact.published_at),
        "observed_at": _datetime_text(fact.observed_at),
        "revision_no": fact.revision_no,
        "adjustment_factor": _decimal_text(fact.adjustment_factor),
        "source_reference": fact.source_reference,
        "raw_content_hash": fact.raw_content_hash,
    }


def _fact_signature(fact: CorporateActionFact) -> str:
    return json.dumps(_stored_fact_payload(fact), sort_keys=True, separators=(",", ":"))


def _json_hash(value: object) -> str:
    serialized = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _datetime_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f")


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _adjustment_unavailable(message: str) -> AppError:
    return AppError(
        code="ADJUSTMENT_DATA_UNAVAILABLE",
        message=message,
        status_code=409,
    )


class QualityIssueRepositoryPort(Protocol):
    @property
    def session(self) -> object: ...

    async def find_by_dedupe_key(self, dedupe_key: str) -> DataQualityIssue | None: ...

    async def get_for_update(self, issue_id: UUID) -> DataQualityIssue | None: ...

    async def claim_issue(
        self, record: DataQualityIssue
    ) -> tuple[DataQualityIssue, bool]: ...

    async def flush(self) -> None: ...

    async def list(
        self,
        *,
        status: QualityIssueStatus | None = None,
        issue_type: str | None = None,
        symbol: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> list[DataQualityIssue]: ...

    async def count(
        self,
        *,
        status: QualityIssueStatus | None = None,
        issue_type: str | None = None,
        symbol: str | None = None,
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class OpenQualityIssueResult:
    issue: QualityIssueView
    created: bool
    replayed: bool


@dataclass(frozen=True, slots=True)
class ResolveQualityIssueResult:
    issue: QualityIssueView
    replayed: bool


class QualityIssueService:
    def __init__(
        self,
        repository: QualityIssueRepositoryPort,
        *,
        events: QualityEventPort | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._events = events
        self._now = now_provider or (lambda: datetime.now(UTC))

    async def open(self, command: OpenQualityIssue) -> OpenQualityIssueResult:
        existing = await self._repository.find_by_dedupe_key(command.dedupe_key)
        if existing is None:
            now = self._now()
            candidate = DataQualityIssue(
                issue_type=command.issue_type,
                subject_type=command.subject_type,
                subject_id=command.subject_id,
                symbol=command.symbol,
                status=(
                    QualityIssueStatus.REVIEW_REQUIRED
                    if command.requires_review
                    else QualityIssueStatus.OPEN
                ),
                severity=command.severity,
                evidence=_json_copy(command.evidence),
                dedupe_key=command.dedupe_key,
                occurrence_count=1,
                first_seen_at=now,
                last_seen_at=now,
            )
            claimed, created = await self._repository.claim_issue(candidate)
            if created:
                return OpenQualityIssueResult(
                    issue=_to_view(claimed),
                    created=True,
                    replayed=False,
                )
            existing = claimed

        locked = await self._repository.get_for_update(existing.id)
        if locked is None:
            raise AppError(
                code="QUALITY_ISSUE_NOT_FOUND",
                message="数据质量问题不存在",
                status_code=404,
            )
        status = _issue_status(locked)
        if status in TERMINAL_STATUSES:
            return OpenQualityIssueResult(
                issue=_to_view(locked),
                created=False,
                replayed=True,
            )

        current_severity = _severity(locked)
        if SEVERITY_RANK[command.severity] > SEVERITY_RANK[current_severity]:
            locked.severity = command.severity
        locked.occurrence_count += 1
        locked.last_seen_at = self._now()
        locked.evidence = _json_copy(command.evidence)
        await self._repository.flush()
        return OpenQualityIssueResult(
            issue=_to_view(locked),
            created=False,
            replayed=True,
        )

    async def resolve(
        self,
        command: ResolveQualityIssue,
    ) -> ResolveQualityIssueResult:
        issue = await self._repository.get_for_update(command.issue_id)
        if issue is None:
            raise AppError(
                code="QUALITY_ISSUE_NOT_FOUND",
                message="数据质量问题不存在",
                status_code=404,
            )
        action = _resolution_action(command.action)
        status = _issue_status(issue)
        if status in TERMINAL_STATUSES:
            if _same_resolution(issue, command, action):
                return ResolveQualityIssueResult(issue=_to_view(issue), replayed=True)
            raise AppError(
                code="QUALITY_ISSUE_STATE_CONFLICT",
                message="数据质量问题已按其他方式处理",
                status_code=409,
            )
        if status not in {
            QualityIssueStatus.OPEN,
            QualityIssueStatus.REVIEW_REQUIRED,
        }:
            raise AppError(
                code="QUALITY_ISSUE_STATE_INVALID",
                message="数据质量问题状态不允许处理",
                status_code=409,
            )
        if action is QualityResolutionAction.REFETCH:
            raise AppError(
                code="QUALITY_ACTION_NOT_ALLOWED",
                message="重新抓取不属于终态裁决",
                status_code=422,
            )
        if action is QualityResolutionAction.SELECT_SOURCE:
            _validate_selected_source(issue, command.selected_source)

        if self._events is None:
            raise AppError(
                code="QUALITY_INTEGRATION_UNAVAILABLE",
                message="数据质量事件集成不可用",
                status_code=503,
            )
        if self._events.session is not self._repository.session:
            raise AppError(
                code="QUALITY_TRANSACTION_MISMATCH",
                message="数据质量裁决与事件不在同一事务中",
                status_code=500,
            )

        issue.status = (
            QualityIssueStatus.INVALIDATED
            if action is QualityResolutionAction.INVALIDATE
            else QualityIssueStatus.RESOLVED
        )
        issue.resolved_at = self._now()
        issue.resolved_by_user_id = command.actor_user_id
        issue.resolution_action = action
        issue.resolution_reason = command.reason
        issue.selected_source = command.selected_source
        await self._repository.flush()
        await self._events.append_resolved(issue)
        return ResolveQualityIssueResult(issue=_to_view(issue), replayed=False)

    async def list(
        self,
        *,
        status: QualityIssueStatus | None = None,
        issue_type: str | None = None,
        symbol: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> QualityIssuePage:
        issues = await self._repository.list(
            status=status,
            issue_type=issue_type,
            symbol=symbol,
            page=page,
            page_size=page_size,
        )
        total = await self._repository.count(
            status=status,
            issue_type=issue_type,
            symbol=symbol,
        )
        return QualityIssuePage(
            items=tuple(_to_view(issue) for issue in issues),
            total=total,
            page=page,
            page_size=page_size,
        )

    async def request_refetch(
        self,
        command: RequestQualityRefetch,
    ) -> QualityIssueView:
        issue = await self._repository.get_for_update(command.issue_id)
        if issue is None:
            raise AppError(
                code="QUALITY_ISSUE_NOT_FOUND",
                message="数据质量问题不存在",
                status_code=404,
            )
        status = _issue_status(issue)
        if status in TERMINAL_STATUSES:
            raise AppError(
                code="QUALITY_ISSUE_STATE_CONFLICT",
                message="已结束的数据质量问题不能请求重新抓取",
                status_code=409,
            )
        if self._events is None:
            raise AppError(
                code="QUALITY_INTEGRATION_UNAVAILABLE",
                message="数据质量事件集成不可用",
                status_code=503,
            )
        if self._events.session is not self._repository.session:
            raise AppError(
                code="QUALITY_TRANSACTION_MISMATCH",
                message="数据质量问题与事件不在同一事务中",
                status_code=500,
            )
        await self._events.append_refetch_requested(issue, command)
        return _to_view(issue)


def _json_copy(value: Mapping[str, object]) -> dict[str, object]:
    return json.loads(json.dumps(value, allow_nan=False))


def _to_view(issue: DataQualityIssue) -> QualityIssueView:
    return QualityIssueView(
        id=issue.id,
        issue_type=issue.issue_type,
        subject_type=issue.subject_type,
        subject_id=issue.subject_id,
        symbol=issue.symbol,
        status=issue.status,
        severity=issue.severity,
        evidence=issue.evidence,
        occurrence_count=issue.occurrence_count,
        first_seen_at=issue.first_seen_at,
        last_seen_at=issue.last_seen_at,
        resolved_at=issue.resolved_at,
        resolved_by_user_id=issue.resolved_by_user_id,
        resolution_action=issue.resolution_action,
        resolution_reason=issue.resolution_reason,
        selected_source=issue.selected_source,
    )


def _issue_status(issue: DataQualityIssue) -> QualityIssueStatus:
    try:
        return QualityIssueStatus(issue.status)
    except (TypeError, ValueError) as exc:
        raise AppError(
            code="QUALITY_ISSUE_STATE_INVALID",
            message="数据质量问题状态无效",
            status_code=409,
        ) from exc


def _severity(issue: DataQualityIssue) -> QualitySeverity:
    try:
        return QualitySeverity(issue.severity)
    except (TypeError, ValueError) as exc:
        raise AppError(
            code="QUALITY_SEVERITY_INVALID",
            message="数据质量问题严重度无效",
            status_code=422,
        ) from exc


def _resolution_action(value: object) -> QualityResolutionAction:
    try:
        return QualityResolutionAction(value)
    except (TypeError, ValueError) as exc:
        raise AppError(
            code="QUALITY_ACTION_NOT_ALLOWED",
            message="不支持的数据质量处理动作",
            status_code=422,
        ) from exc


def _same_resolution(
    issue: DataQualityIssue,
    command: ResolveQualityIssue,
    action: QualityResolutionAction,
) -> bool:
    return (
        issue.resolution_action == action
        and issue.resolved_by_user_id == command.actor_user_id
        and issue.resolution_reason == command.reason
        and issue.selected_source == command.selected_source
    )


def _validate_selected_source(
    issue: DataQualityIssue,
    selected_source: str | None,
) -> None:
    if not isinstance(issue.evidence, Mapping):
        raise AppError(
            code="QUALITY_EVIDENCE_INVALID",
            message="数据质量证据必须是对象",
            status_code=422,
        )
    sources = issue.evidence.get("sources")
    if not isinstance(sources, Mapping):
        raise AppError(
            code="QUALITY_EVIDENCE_INVALID",
            message="数据质量证据中的来源必须是对象",
            status_code=422,
        )
    if selected_source not in sources:
        raise AppError(
            code="QUALITY_SOURCE_NOT_AVAILABLE",
            message="选择的来源不在已保存证据中",
            status_code=422,
        )
