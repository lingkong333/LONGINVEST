from __future__ import annotations

from collections.abc import Callable
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
    DailyBarRevision,
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

        inserted = unchanged = revised = review_required = 0
        for item in ordered:
            previous_close = await self._repository.get_previous_close(
                item.security_id, item.trade_date
            )
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
            existing = await self._repository.get_bar(item.security_id, item.trade_date)
            stage = DailyBarStage(
                id=uuid4(),
                batch_id=uuid4(),
                security_id=item.security_id,
                symbol=item.symbol,
                trading_date=item.trade_date,
                status=(
                    DailyStageStatus.REVIEW_REQUIRED
                    if quality.review_required
                    else DailyStageStatus.VALID
                ),
                provider_payload=payload,
                received_at=self._now(),
                validated_at=self._now(),
                expires_at=self._now() + timedelta(days=7),
            )
            revision_no = await self._commit_stage(stage, revision_reason=reason)
            if existing is None:
                inserted += 1
            elif revision_no:
                revised += 1
            else:
                unchanged += 1
            if quality.review_required:
                review_required += 1
                await self._quality_service().open(
                    OpenQualityIssue(
                        issue_type=quality.code,
                        subject_type="daily_bar_unadjusted",
                        subject_id=f"{item.security_id}:{item.trade_date}",
                        symbol=item.symbol,
                        severity=QualitySeverity.WARNING,
                        evidence={
                            "security_id": str(item.security_id),
                            "symbol": item.symbol,
                            "trade_date": item.trade_date.isoformat(),
                            "quality_code": quality.code,
                            "source": item.source,
                        },
                        dedupe_key=(
                            f"history-daily-review:{item.security_id}:"
                            f"{item.trade_date}:{quality.code}"
                        ),
                        requires_review=True,
                    )
                )
        return HistoricalDailyStoreResult(
            inserted=inserted,
            unchanged=unchanged,
            revised=revised,
            review_required=review_required,
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
                previous_close = await self._repository.get_previous_close(
                    stage.security_id, batch.trading_date
                )
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
            try:
                async with self._repository.item_savepoint():
                    changed = await self._commit_stage(stage)
                    if changed:
                        await self._event_writer().append(
                            topic="daily_bar.corrected",
                            aggregate_id=f"{stage.security_id}:{stage.trading_date}",
                            payload={
                                "event_type": "daily_bar.corrected",
                                "security_id": str(stage.security_id),
                                "symbol": stage.symbol,
                                "trade_date": stage.trading_date.isoformat(),
                            },
                            dedupe_key=f"daily-bar-corrected:{stage.security_id}:{stage.trading_date}:{changed}",
                        )
                committed_symbols.append(symbol)
            except Exception as exc:
                missing.append(
                    _missing(
                        batch.id,
                        symbol,
                        stage.security_id,
                        DailyMissingReason.UNEXPLAINED,
                        _failure_code(exc),
                        self._now(),
                    )
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

    async def _commit_stage(
        self, stage: DailyBarStage, *, revision_reason: str = "provider_replay_changed"
    ) -> int:
        values = _bar_values(stage)
        await self._repository.lock_bar_key(stage.security_id, stage.trading_date)
        existing = await self._repository.get_bar(stage.security_id, stage.trading_date)
        if existing is None:
            await self._repository.add_bar(
                DailyBarUnadjusted(
                    security_id=stage.security_id,
                    trade_date=stage.trading_date,
                    symbol=stage.symbol,
                    data_version=1,
                    created_at=self._now(),
                    updated_at=self._now(),
                    **values,
                )
            )
            return 0
        old_values = _stored_values(existing)
        changed_fields = tuple(
            key for key, value in values.items() if old_values[key] != value
        )
        if not changed_fields:
            return 0
        revision_no = await self._repository.next_revision_no(
            stage.security_id, stage.trading_date
        )
        await self._repository.add_revision(
            DailyBarRevision(
                id=uuid4(),
                daily_bar_security_id=stage.security_id,
                daily_bar_trade_date=stage.trading_date,
                symbol=stage.symbol,
                revision_no=revision_no,
                old_values=_json_values(old_values),
                new_values=_json_values(values),
                changed_fields=list(changed_fields),
                source=values["source"],
                reason=revision_reason,
                created_at=self._now(),
            )
        )
        for key, value in values.items():
            setattr(existing, key, value)
        existing.data_version += 1
        existing.updated_at = self._now()
        await self._repository.flush()
        return revision_no

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
    }


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


def _failure_code(exc: Exception) -> str:
    text = str(exc).lower()
    if "partition" in text:
        return "DAILY_PARTITION_UNAVAILABLE"
    return "DAILY_BAR_COMMIT_FAILED"
