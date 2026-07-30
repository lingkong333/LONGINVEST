from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from uuid import UUID, uuid4

from long_invest.modules.daily_data.contracts import (
    CreateDailyBatch,
    DailyBatchAction,
    DailyBatchStatus,
    DailyBatchSummary,
    DailyMissingReason,
    DailyStageStatus,
    HistoricalDailyBarInput,
    HistoricalDailyStoreResult,
    StageDailyBar,
)
from long_invest.modules.daily_data.models import (
    DailyBarStage,
    DailyBarUnadjusted,
    DailyBatchMissingItem,
)
from long_invest.modules.daily_data.quality import (
    DailyQualityContext,
    validate_daily_bar,
)
from long_invest.modules.market_data.contracts import OpenQualityIssue, QualitySeverity
from long_invest.platform.errors import AppError


def daily_batch_allowed_actions(
    status: DailyBatchStatus | str,
    *,
    missing_count: int,
    failed_count: int,
) -> tuple[DailyBatchAction, ...]:
    normalized = DailyBatchStatus(str(status))
    if (
        normalized in {DailyBatchStatus.PARTIAL, DailyBatchStatus.FAILED}
        and missing_count + failed_count > 0
    ):
        return (DailyBatchAction.RETRY_MISSING,)
    return ()


class DailyEventPort(Protocol):
    async def append(
        self,
        *,
        topic: str,
        aggregate_id: str,
        payload: dict[str, Any],
        dedupe_key: str,
    ) -> None: ...


class QualityIssuePort(Protocol):
    async def open(self, command: OpenQualityIssue) -> object: ...


class DailyDataService:
    def __init__(
        self,
        repository: Any,
        *,
        events: DailyEventPort | None = None,
        quality_issues: QualityIssuePort | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._events = events
        self._quality_issues = quality_issues
        self._now = now_provider or (lambda: datetime.now(UTC))

    async def create(self, command: CreateDailyBatch) -> DailyBatchSummary:
        batch, _created = await self._repository.claim_batch(command, self._now())
        return _summary(batch)

    async def store_historical_bars(
        self,
        bars: tuple[HistoricalDailyBarInput, ...],
        *,
        reason: str,
    ) -> HistoricalDailyStoreResult:
        if not bars:
            raise ValueError("historical daily bars cannot be empty")
        if not reason.strip() or len(reason) > 500:
            raise ValueError("historical daily revision reason is invalid")
        ordered = tuple(sorted(bars, key=lambda item: item.trade_date))
        if len({(item.security_id, item.trade_date) for item in ordered}) != len(
            ordered
        ):
            raise ValueError("historical daily bars contain duplicate dates")

        security_ids = {item.security_id for item in ordered}
        symbols = {item.symbol for item in ordered}
        if len(security_ids) != 1 or len(symbols) != 1:
            raise ValueError("historical daily bars must belong to one security")

        security_id = ordered[0].security_id
        await self._repository.lock_security_bars(security_id)
        existing_by_date = {
            item.trade_date: item
            for item in await self._repository.list_bars_in_window(
                security_id,
                start=ordered[0].trade_date,
                end=ordered[-1].trade_date,
            )
        }
        previous_close = await self._repository.get_previous_close(
            security_id, ordered[0].trade_date
        )
        now = self._now()
        desired_by_date: dict[date, dict[str, Any]] = {}
        review_items: list[tuple[HistoricalDailyBarInput, str]] = []
        for item in ordered:
            payload = {
                "symbol": item.symbol,
                "trading_date": item.trade_date,
                "open": item.open,
                "high": item.high,
                "low": item.low,
                "close": item.close,
                "previous_close": previous_close,
                "volume": item.volume,
                "amount": item.amount,
                "source": item.source,
            }
            quality = validate_daily_bar(
                payload,
                expected_symbol=item.symbol,
                expected_date=item.trade_date,
                context=DailyQualityContext(
                    is_new_listing=previous_close is None,
                    is_st=False,
                    has_known_corporate_action=False,
                    previous_close=previous_close,
                ),
                seen_keys=set(),
            )
            if not quality.valid:
                raise AppError(
                    code=quality.code,
                    message="历史日线数据未通过正式数据校验",
                    status_code=422,
                )
            desired_by_date[item.trade_date] = {
                "security_id": item.security_id,
                "trade_date": item.trade_date,
                "symbol": item.symbol,
                "open": item.open,
                "high": item.high,
                "low": item.low,
                "close": item.close,
                "previous_close": previous_close,
                "volume": item.volume,
                "amount": item.amount,
                "source": item.source,
                "source_identity": _source_identity(item.source, item.source_identity),
                "collected_at": item.collected_at or now,
            }
            if quality.review_required:
                review_items.append((item, quality.code))
            previous_close = item.close

        inserted_dates: list[date] = []
        changed_dates: list[date] = []
        for trade_date, values in desired_by_date.items():
            existing = existing_by_date.get(trade_date)
            if existing is None:
                inserted_dates.append(trade_date)
            elif (
                any(
                    _stored_values(existing)[key] != value
                    for key, value in values.items()
                    if key not in {"security_id", "trade_date", "symbol"}
                )
                or existing.symbol != values["symbol"]
            ):
                changed_dates.append(trade_date)

        revision_numbers = await self._repository.latest_revision_numbers(
            security_id, changed_dates
        )
        write_rows: list[dict[str, Any]] = []
        revisions: list[dict[str, Any]] = []
        for trade_date in (*inserted_dates, *changed_dates):
            values = desired_by_date[trade_date]
            existing = existing_by_date.get(trade_date)
            data_version = 1 if existing is None else existing.data_version + 1
            write_rows.append(
                {
                    **values,
                    "data_version": data_version,
                    "created_at": existing.created_at if existing is not None else now,
                    "updated_at": now,
                }
            )
            if existing is None:
                continue
            old_values = _stored_values(existing)
            new_values = {
                key: value
                for key, value in values.items()
                if key not in {"security_id", "trade_date", "symbol"}
            }
            changed_fields = tuple(
                key for key, value in new_values.items() if old_values[key] != value
            )
            revisions.append(
                {
                    "id": uuid4(),
                    "daily_bar_security_id": security_id,
                    "daily_bar_trade_date": trade_date,
                    "symbol": values["symbol"],
                    "revision_no": revision_numbers.get(trade_date, 0) + 1,
                    "old_values": _json_values(old_values),
                    "new_values": _json_values(new_values),
                    "changed_fields": list(changed_fields),
                    "source": values["source"],
                    "reason": reason,
                    "created_at": now,
                }
            )
        await self._repository.upsert_historical_bars(write_rows)
        await self._repository.add_historical_revisions(revisions)

        for item, quality_code in review_items:
            await self._quality_service().open(
                OpenQualityIssue(
                    issue_type=quality_code,
                    subject_type="daily_bar_unadjusted",
                    subject_id=f"{item.security_id}:{item.trade_date}",
                    symbol=item.symbol,
                    severity=QualitySeverity.WARNING,
                    evidence={
                        "security_id": str(item.security_id),
                        "symbol": item.symbol,
                        "trade_date": item.trade_date.isoformat(),
                        "quality_code": quality_code,
                        "source": item.source,
                    },
                    dedupe_key=(
                        f"history-daily-review:{item.security_id}:"
                        f"{item.trade_date}:{quality_code}"
                    ),
                    requires_review=True,
                )
            )
        return HistoricalDailyStoreResult(
            inserted=len(inserted_dates),
            unchanged=len(ordered) - len(inserted_dates) - len(changed_dates),
            revised=len(changed_dates),
            review_required=len(review_items),
        )

    async def stage(self, batch_id: UUID, item: StageDailyBar) -> None:
        batch = await self._batch(batch_id, for_update=True)
        status = DailyBatchStatus(batch.status)
        if status not in {DailyBatchStatus.PENDING, DailyBatchStatus.FETCHING}:
            raise AppError(
                code="DAILY_BATCH_STATE_CONFLICT",
                message="日线批次当前状态不允许暂存",
                status_code=409,
                details={"status": status.value},
            )
        if item.symbol not in batch.symbols:
            raise AppError(
                code="DAILY_BAR_SYMBOL_MISMATCH",
                message="股票不在批次冻结范围内",
                status_code=422,
            )
        if item.trading_date != batch.trading_date:
            raise AppError(
                code="DAILY_BAR_DATE_MISMATCH",
                message="日线日期与批次日期不一致",
                status_code=422,
            )
        frozen_security_id = dict(zip(batch.symbols, batch.security_ids, strict=True))[
            item.symbol
        ]
        if str(item.security_id) != str(frozen_security_id):
            raise AppError(
                code="DAILY_BAR_SECURITY_MISMATCH",
                message="股票编号与批次冻结范围不一致",
                status_code=422,
            )
        await self._repository.upsert_stage(
            batch_id, item, self._now() + timedelta(days=7)
        )
        batch.status = DailyBatchStatus.FETCHING
        batch.started_at = batch.started_at or self._now()
        stages = await self._repository.list_stages(batch_id)
        batch.fetched_count = len(stages)
        await self._repository.flush()

    async def stage_many(
        self,
        batch_id: UUID,
        items: tuple[StageDailyBar, ...],
        *,
        requested_count: int,
    ) -> DailyBatchSummary:
        if not items:
            raise ValueError("批量暂存不能为空")
        batch = await self._batch(batch_id, for_update=True)
        status = DailyBatchStatus(batch.status)
        if status not in {
            DailyBatchStatus.PENDING,
            DailyBatchStatus.FETCHING,
            DailyBatchStatus.VALIDATING,
        }:
            raise AppError(
                code="DAILY_BATCH_STATE_CONFLICT",
                message="日线批次当前状态不允许暂存",
                status_code=409,
                details={"status": status.value},
            )
        frozen = dict(zip(batch.symbols, batch.security_ids, strict=True))
        seen = set()
        for item in items:
            if (
                item.symbol in seen
                or item.symbol not in frozen
                or str(item.security_id) != str(frozen.get(item.symbol))
                or item.trading_date != batch.trading_date
            ):
                raise AppError(
                    code="DAILY_STAGE_BATCH_INVALID",
                    message="批量暂存与冻结范围不一致",
                    status_code=422,
                )
            seen.add(item.symbol)
        if requested_count < 0:
            raise ValueError("日线采集进度不能为负数")
        await self._repository.upsert_stages(
            batch_id, items, self._now() + timedelta(days=7)
        )
        batch.status = DailyBatchStatus.FETCHING
        batch.started_at = batch.started_at or self._now()
        batch.fetched_count = await self._repository.count_stages(batch_id)
        batch.requested_count = min(requested_count, batch.expected_count)
        batch.pending_retry_count = await self._repository.count_retryable_stages(
            batch_id
        )
        await self._repository.flush()
        return _summary(batch)

    async def validate(self, batch_id: UUID) -> DailyBatchSummary:
        batch = await self._batch(batch_id, for_update=True)
        status = DailyBatchStatus(batch.status)
        if status in {
            DailyBatchStatus.SUCCEEDED,
            DailyBatchStatus.PARTIAL,
            DailyBatchStatus.FAILED,
        }:
            return _summary(batch)
        if status not in {
            DailyBatchStatus.PENDING,
            DailyBatchStatus.FETCHING,
            DailyBatchStatus.VALIDATING,
        }:
            raise AppError(
                code="DAILY_BATCH_STATE_CONFLICT",
                message="日线批次当前状态不允许校验",
                status_code=409,
                details={"status": status.value},
            )
        stages = await self._repository.list_stages(batch_id)
        batch.status = DailyBatchStatus.VALIDATING
        seen: set[tuple[str, object]] = set()
        validated = 0
        needs_previous_close = [
            stage.security_id
            for stage in stages
            if DailyStageStatus(stage.status) is DailyStageStatus.FETCHED
            and "previous_close" not in (stage.provider_payload or {})
        ]
        previous_closes = await self._repository.get_previous_closes(
            needs_previous_close, batch.trading_date
        )
        for stage in stages:
            if DailyStageStatus(stage.status) is not DailyStageStatus.FETCHED:
                if DailyStageStatus(stage.status) in {
                    DailyStageStatus.VALID,
                    DailyStageStatus.REVIEW_REQUIRED,
                }:
                    validated += 1
                continue
            payload = _restore_provider_payload(stage.provider_payload or {})
            is_new_listing = bool(payload.pop("is_new_listing", False))
            is_st = bool(payload.pop("is_st", False))
            has_known_corporate_action = bool(
                payload.pop("has_known_corporate_action", False)
            )
            payload.pop("source_identity", None)
            payload.pop("collected_at", None)
            if "previous_close" in payload:
                try:
                    previous_close = _positive_previous_close(payload["previous_close"])
                except (InvalidOperation, TypeError, ValueError):
                    _invalidate_stage(
                        stage,
                        "DAILY_BAR_PREVIOUS_CLOSE_INVALID",
                        self._now(),
                    )
                    continue
            else:
                previous_close = previous_closes.get(stage.security_id)
                if previous_close is None:
                    if not is_new_listing:
                        _invalidate_stage(
                            stage,
                            "DAILY_BAR_PREVIOUS_CLOSE_MISSING",
                            self._now(),
                        )
                        continue
                else:
                    try:
                        previous_close = _positive_previous_close(previous_close)
                    except (InvalidOperation, TypeError, ValueError):
                        _invalidate_stage(
                            stage,
                            "DAILY_BAR_PREVIOUS_CLOSE_INVALID",
                            self._now(),
                        )
                        continue
            if previous_close is not None:
                payload["previous_close"] = previous_close
                stage.provider_payload = {
                    **(stage.provider_payload or {}),
                    "previous_close": str(previous_close),
                }
            result = validate_daily_bar(
                payload,
                expected_symbol=stage.symbol,
                expected_date=batch.trading_date,
                context=DailyQualityContext(
                    is_new_listing=is_new_listing,
                    is_st=is_st,
                    has_known_corporate_action=has_known_corporate_action,
                    previous_close=previous_close,
                ),
                seen_keys=seen,
            )
            stage.quality_code = result.code
            stage.validated_at = self._now()
            if result.valid:
                stage.status = (
                    DailyStageStatus.REVIEW_REQUIRED
                    if result.review_required
                    else DailyStageStatus.VALID
                )
                validated += 1
                seen.add((stage.symbol, stage.trading_date))
                if result.review_required:
                    await self._open_review_issue(batch, stage, result.code)
            else:
                stage.status = DailyStageStatus.INVALID
                stage.error_code = result.code
        batch.validated_count = validated
        batch.pending_retry_count = await self._repository.count_retryable_stages(
            batch_id
        )
        await self._repository.flush()
        return _summary(batch)

    async def commit(self, batch_id: UUID) -> DailyBatchSummary:
        batch = await self._batch(batch_id, for_update=True)
        current_status = DailyBatchStatus(batch.status)
        if current_status in {
            DailyBatchStatus.SUCCEEDED,
            DailyBatchStatus.PARTIAL,
            DailyBatchStatus.FAILED,
        }:
            return _summary(batch)
        if current_status is not DailyBatchStatus.VALIDATING:
            raise AppError(
                code="DAILY_BATCH_STATE_CONFLICT",
                message="日线批次必须完成校验后才能提交",
                status_code=409,
                details={"status": current_status.value},
            )
        batch.status = DailyBatchStatus.COMMITTING
        stages = await self._repository.list_stages(batch_id)
        committed_symbols: list[str] = []
        missing: list[DailyBatchMissingItem] = []
        ready_stages: list[DailyBarStage] = []

        staged_by_symbol = {item.symbol: item for item in stages}
        for symbol in batch.symbols:
            stage = staged_by_symbol.get(symbol)
            if stage is None:
                missing.append(
                    _missing(
                        batch.id,
                        symbol,
                        None,
                        DailyMissingReason.UNEXPLAINED,
                        "DAILY_MISSING_UNEXPLAINED",
                        self._now(),
                    )
                )
                continue
            status = DailyStageStatus(stage.status)
            if status is DailyStageStatus.MISSING:
                reason = DailyMissingReason(stage.missing_reason)
                missing.append(
                    _missing(
                        batch.id,
                        symbol,
                        stage.security_id,
                        reason,
                        stage.error_code,
                        self._now(),
                    )
                )
                continue
            if status not in {DailyStageStatus.VALID, DailyStageStatus.REVIEW_REQUIRED}:
                missing.append(
                    _missing(
                        batch.id,
                        symbol,
                        stage.security_id,
                        DailyMissingReason.UNEXPLAINED,
                        stage.error_code or "DAILY_BAR_INVALID",
                        self._now(),
                    )
                )
                continue
            if stage.validated_at is None:
                missing.append(
                    _missing(
                        batch.id,
                        symbol,
                        stage.security_id,
                        DailyMissingReason.UNEXPLAINED,
                        "DAILY_BAR_NOT_VALIDATED",
                        self._now(),
                    )
                )
                continue
            ready_stages.append(stage)

        bulk_committed, bulk_missing = await self._commit_stages(ready_stages)
        committed_symbols.extend(bulk_committed)
        missing.extend(
            _missing(
                batch.id,
                stage.symbol,
                stage.security_id,
                DailyMissingReason.UNEXPLAINED,
                error_code,
                self._now(),
            )
            for stage, error_code in bulk_missing
        )

        await self._repository.replace_missing(batch.id, missing)
        unexplained = [item for item in missing if not item.explained]
        for item in unexplained:
            await self._quality_service().open(
                OpenQualityIssue(
                    issue_type="DAILY_MISSING_UNEXPLAINED",
                    subject_type="daily_data_batch",
                    subject_id=str(batch.id),
                    symbol=item.symbol,
                    severity=QualitySeverity.ERROR,
                    evidence={
                        "batch_id": str(batch.id),
                        "universe_snapshot_id": str(batch.universe_snapshot_id),
                        "symbol": item.symbol,
                        "error_code": item.error_code,
                    },
                    dedupe_key=f"daily-missing:{batch.id}:{item.symbol}",
                )
            )

        batch.committed_count = len(committed_symbols)
        batch.validated_count = sum(
            DailyStageStatus(item.status)
            in {DailyStageStatus.VALID, DailyStageStatus.REVIEW_REQUIRED}
            for item in stages
        )
        batch.missing_count = len(missing)
        batch.failed_count = len(unexplained)
        batch.pending_retry_count = 0
        if not unexplained:
            batch.status = DailyBatchStatus.SUCCEEDED
        elif committed_symbols:
            batch.status = DailyBatchStatus.PARTIAL
        else:
            batch.status = DailyBatchStatus.FAILED
        batch.completed_at = (
            self._now() if batch.status is not DailyBatchStatus.FAILED else None
        )
        await self._repository.flush()

        if batch.status in {DailyBatchStatus.SUCCEEDED, DailyBatchStatus.PARTIAL}:
            topic = (
                "daily_batch.completed"
                if batch.status is DailyBatchStatus.SUCCEEDED
                else "daily_batch.partial"
            )
            await self._event_writer().append(
                topic=topic,
                aggregate_id=str(batch.id),
                payload={
                    "event_type": topic,
                    "batch_id": str(batch.id),
                    "trading_date": batch.trading_date.isoformat(),
                    "universe_snapshot_id": str(batch.universe_snapshot_id),
                    "valid_symbols": committed_symbols,
                    "missing": [
                        {
                            "symbol": item.symbol,
                            "reason": str(item.reason),
                            "explained": item.explained,
                        }
                        for item in missing
                    ],
                },
                dedupe_key=f"{topic}:{batch.id}",
            )
        return _summary(batch)

    async def _commit_stages(
        self, stages: list[DailyBarStage]
    ) -> tuple[list[str], list[tuple[DailyBarStage, str]]]:
        if not stages:
            return [], []
        trade_date = stages[0].trading_date
        existing_by_key = {
            (item.security_id, item.trade_date): item
            for item in await self._repository.list_bars_for_update(
                [stage.security_id for stage in stages], trade_date
            )
        }
        changed_ids: list[UUID] = []
        prepared: list[tuple[DailyBarStage, dict[str, Any], Any, tuple[str, ...]]] = []
        committed = []
        now = self._now()
        for stage in stages:
            key = (stage.security_id, stage.trading_date)
            values = _bar_values(stage)
            existing = existing_by_key.get(key)
            changed_fields = (
                tuple(values)
                if existing is None
                else tuple(
                    field
                    for field, value in values.items()
                    if _stored_values(existing)[field] != value
                )
            )
            if existing is not None and not changed_fields:
                committed.append(stage.symbol)
                continue
            if existing is not None:
                changed_ids.append(stage.security_id)
            prepared.append((stage, values, existing, changed_fields))

        revision_numbers = await self._repository.latest_revision_numbers_for_date(
            changed_ids, trade_date
        )
        rows: list[dict[str, Any]] = []
        revisions: dict[tuple[UUID, date], dict[str, Any]] = {}
        changed_revision: dict[tuple[UUID, date], int] = {}
        stage_by_key = {}
        for stage, values, existing, changed_fields in prepared:
            key = (stage.security_id, stage.trading_date)
            stage_by_key[key] = stage
            rows.append(
                {
                    "security_id": stage.security_id,
                    "trade_date": stage.trading_date,
                    "symbol": stage.symbol,
                    **values,
                    "data_version": (
                        1 if existing is None else existing.data_version + 1
                    ),
                    "created_at": existing.created_at if existing is not None else now,
                    "updated_at": now,
                }
            )
            if existing is None:
                continue
            revision_no = revision_numbers.get(stage.security_id, 0) + 1
            changed_revision[key] = revision_no
            revisions[key] = {
                "id": uuid4(),
                "daily_bar_security_id": stage.security_id,
                "daily_bar_trade_date": stage.trading_date,
                "symbol": stage.symbol,
                "revision_no": revision_no,
                "old_values": _json_values(_stored_values(existing)),
                "new_values": _json_values(values),
                "changed_fields": list(changed_fields),
                "source": values["source"],
                "reason": "provider_replay_changed",
                "created_at": now,
            }

        succeeded, failures = await self._repository.store_current_daily_bars(
            rows, revisions
        )
        committed.extend(stage_by_key[key].symbol for key in succeeded)
        for key in succeeded:
            revision_no = changed_revision.get(key)
            if revision_no is None:
                continue
            stage = stage_by_key[key]
            await self._event_writer().append(
                topic="daily_bar.corrected",
                aggregate_id=f"{stage.security_id}:{stage.trading_date}",
                payload={
                    "event_type": "daily_bar.corrected",
                    "security_id": str(stage.security_id),
                    "symbol": stage.symbol,
                    "trade_date": stage.trading_date.isoformat(),
                },
                dedupe_key=(
                    f"daily-bar-corrected:{stage.security_id}:"
                    f"{stage.trading_date}:{revision_no}"
                ),
            )
        failed = [(stage_by_key[key], code) for key, code in failures.items()]
        return committed, failed

    async def retry_scope(self, batch_id: UUID) -> tuple[str, ...]:
        batch = await self._batch(batch_id, for_update=True)
        status = DailyBatchStatus(batch.status)
        if status not in {DailyBatchStatus.PARTIAL, DailyBatchStatus.FAILED}:
            raise AppError(
                code="DAILY_RETRY_STATE_CONFLICT",
                message="日线批次当前状态不允许重试",
                status_code=409,
                details={"status": status.value},
            )
        stages = {
            item.symbol: item for item in await self._repository.list_stages(batch_id)
        }
        missing = await self._repository.list_all_missing(batch_id)
        retry_symbols = {
            item.symbol
            for item in missing
            if not item.explained
            or DailyMissingReason(item.reason) is DailyMissingReason.UNEXPLAINED
        }
        for symbol in batch.symbols:
            item = stages.get(symbol)
            if item is None:
                retry_symbols.add(symbol)
                continue
            status = DailyStageStatus(item.status)
            if (
                status in {DailyStageStatus.FAILED, DailyStageStatus.INVALID}
                or status is DailyStageStatus.MISSING
                and DailyMissingReason(item.missing_reason)
                is DailyMissingReason.UNEXPLAINED
            ):
                retry_symbols.add(symbol)
        return tuple(symbol for symbol in batch.symbols if symbol in retry_symbols)

    async def _batch(self, batch_id: UUID, *, for_update: bool = False):
        batch = await self._repository.get_batch(batch_id, for_update=for_update)
        if batch is None:
            raise AppError(
                code="DAILY_BATCH_NOT_FOUND",
                message="日线批次不存在",
                status_code=404,
            )
        return batch

    async def _open_review_issue(
        self, batch: Any, stage: DailyBarStage, code: str
    ) -> None:
        await self._quality_service().open(
            OpenQualityIssue(
                issue_type=code,
                subject_type="daily_bar_stage",
                subject_id=str(stage.id),
                symbol=stage.symbol,
                severity=QualitySeverity.WARNING,
                evidence={
                    "batch_id": str(batch.id),
                    "universe_snapshot_id": str(batch.universe_snapshot_id),
                    "symbol": stage.symbol,
                    "trade_date": stage.trading_date.isoformat(),
                    "quality_code": code,
                },
                dedupe_key=f"daily-review:{batch.id}:{stage.symbol}:{code}",
                requires_review=True,
            )
        )

    def _event_writer(self) -> DailyEventPort:
        if self._events is None:
            raise AppError(
                code="DAILY_INTEGRATION_UNAVAILABLE",
                message="日线可靠事件集成不可用",
                status_code=503,
            )
        return self._events

    def _quality_service(self) -> QualityIssuePort:
        if self._quality_issues is None:
            raise AppError(
                code="DAILY_INTEGRATION_UNAVAILABLE",
                message="日线质量问题集成不可用",
                status_code=503,
            )
        return self._quality_issues


def _summary(batch: Any) -> DailyBatchSummary:
    return DailyBatchSummary(
        id=batch.id,
        trading_date=batch.trading_date,
        universe_snapshot_id=batch.universe_snapshot_id,
        status=batch.status,
        expected_count=batch.expected_count,
        fetched_count=batch.fetched_count,
        validated_count=batch.validated_count,
        committed_count=batch.committed_count,
        missing_count=batch.missing_count,
        failed_count=batch.failed_count,
        requested_count=getattr(batch, "requested_count", 0) or 0,
        pending_retry_count=getattr(batch, "pending_retry_count", 0) or 0,
        plan_snapshot=getattr(batch, "plan_snapshot", {}) or {},
        created_at=batch.created_at,
        started_at=batch.started_at,
        deadline_at=batch.deadline_at,
        completed_at=batch.completed_at,
    )


def _bar_values(stage: DailyBarStage) -> dict[str, Any]:
    payload = stage.provider_payload or {}
    return {
        "open": Decimal(str(payload["open"])),
        "high": Decimal(str(payload["high"])),
        "low": Decimal(str(payload["low"])),
        "close": Decimal(str(payload["close"])),
        "previous_close": _optional_decimal(payload.get("previous_close")),
        "volume": int(payload["volume"]),
        "amount": Decimal(str(payload["amount"])),
        "source": str(payload["source"]),
        "source_identity": _source_identity(
            str(payload["source"]), payload.get("source_identity")
        ),
        "collected_at": _collected_at(payload.get("collected_at"), stage.received_at),
    }


def _stored_values(bar: DailyBarUnadjusted) -> dict[str, Any]:
    return {
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "previous_close": bar.previous_close,
        "volume": bar.volume,
        "amount": bar.amount,
        "source": bar.source,
        "source_identity": bar.source_identity,
        "collected_at": bar.collected_at,
    }


def _source_identity(source: str, value: object) -> dict[str, str]:
    if isinstance(value, Mapping):
        required = {
            "adapter",
            "upstream",
            "interface",
            "capability",
            "algorithm_version",
        }
        result = {key: str(item) for key, item in value.items()}
        if required <= result.keys() and all(result[key].strip() for key in required):
            return result
    return {
        "adapter": "DIRECT_IMPORT",
        "upstream": source,
        "interface": "daily-data-service",
        "capability": "HISTORICAL_DAILY_UNADJUSTED",
        "algorithm_version": "raw-v1",
    }


def _collected_at(value: object, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed
    return fallback


def _json_values(values: dict[str, Any]) -> dict[str, Any]:
    return {
        key: str(value) if isinstance(value, Decimal) else value
        for key, value in values.items()
    }


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _positive_previous_close(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError
    return result


def _invalidate_stage(stage: DailyBarStage, code: str, now: datetime) -> None:
    stage.status = DailyStageStatus.INVALID
    stage.quality_code = code
    stage.error_code = code
    stage.validated_at = now


def _restore_provider_payload(payload: dict[str, Any]) -> dict[str, Any]:
    restored = dict(payload)
    trading_date = restored.get("trading_date")
    if isinstance(trading_date, str):
        with suppress(ValueError):
            restored["trading_date"] = date.fromisoformat(trading_date)
    for field in ("open", "high", "low", "close", "previous_close", "amount"):
        value = restored.get(field)
        if value is None or isinstance(value, bool):
            continue
        with suppress(InvalidOperation, TypeError, ValueError):
            restored[field] = Decimal(str(value))
    volume = restored.get("volume")
    if isinstance(volume, str):
        with suppress(ValueError):
            restored["volume"] = int(volume)
    return restored


def _missing(batch_id, symbol, security_id, reason, error_code, now):
    return DailyBatchMissingItem(
        id=uuid4(),
        batch_id=batch_id,
        security_id=security_id,
        symbol=symbol,
        reason=reason,
        error_code=error_code,
        explained=reason.explained,
        created_at=now,
    )
