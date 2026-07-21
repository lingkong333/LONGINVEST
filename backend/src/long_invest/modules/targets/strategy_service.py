from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from long_invest.modules.targets.contracts import (
    TargetCalculationErrorCode,
    TargetCalculationRunView,
    TargetCalculationStatus,
    TargetReviewStatus,
    TargetReviewView,
    TargetSource,
    TargetStatus,
    TargetValues,
)
from long_invest.modules.targets.models import (
    TargetCalculationRun,
    TargetReview,
    TargetRevision,
)
from long_invest.modules.targets.outbox import TargetEvent
from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.errors import AppError


@dataclass(frozen=True, slots=True)
class CalculateTargetCommand:
    subscription_id: UUID
    target_date: date
    training_start_date: date
    training_end_date: date
    reason: str
    expected_version: int
    idempotency_key: str
    request_id: str
    actor_user_id: str
    session_id: str
    trusted_ip: str


@dataclass(frozen=True, slots=True)
class CalculationReservation:
    run_id: UUID
    replayed: bool
    subscription_id: UUID
    security_id: UUID
    symbol: str
    subscription_version: int
    subscription_revision_id: UUID
    strategy_version_id: UUID
    parameter_snapshot: Mapping[str, Any]
    status: str


@dataclass(frozen=True, slots=True)
class CalculationResult:
    code: str
    run_id: UUID
    revision_id: UUID | None = None
    review_id: UUID | None = None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class ReviewCommand:
    review_id: UUID
    comment: str
    expected_version: int
    idempotency_key: str
    request_id: str
    actor_user_id: str
    session_id: str
    trusted_ip: str
    current_data_version: int | None = None


@dataclass(frozen=True, slots=True)
class ApplyStrategyTargetCommand:
    calculation: CalculateTargetCommand
    strategy_version_id: UUID
    parameter_snapshot: Mapping[str, Any]
    expected_subscription_version: int


class StrategyTargetService:
    """Owns target calculation state transitions; forecast execution stays outside."""

    def __init__(
        self,
        repository,
        *,
        subscriptions,
        audit,
        events,
        large_change_threshold: Decimal = Decimal("0.30"),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not Decimal("0.10") <= large_change_threshold <= Decimal("1.00"):
            raise ValueError("large change threshold must be between 0.10 and 1.00")
        self._repository = repository
        self._subscriptions = subscriptions
        self._audit = audit
        self._events = events
        self._threshold = large_change_threshold
        self._now = now or (lambda: datetime.now(UTC))

    async def reserve(self, command: CalculateTargetCommand) -> CalculationReservation:
        if command.training_start_date > command.training_end_date:
            raise _error("TARGET_TRAINING_RANGE_INVALID", "训练区间无效", 422)
        subscription = await self._subscriptions.lock(command.subscription_id)
        if subscription is None:
            raise _error("TARGET_SUBSCRIPTION_NOT_FOUND", "监控订阅不存在", 404)
        if str(subscription.status) == "ARCHIVED":
            raise _error("TARGET_SUBSCRIPTION_ARCHIVED", "监控订阅已归档", 409)
        if (
            subscription.target_mode != "STRATEGY"
            or subscription.strategy_version_id is None
        ):
            raise _error("TARGET_STRATEGY_NOT_CONFIGURED", "订阅未配置策略目标", 409)
        digest = _digest(command)
        existing = await self._repository.get_calculation_by_idempotency(
            command.subscription_id, command.idempotency_key
        )
        if existing is not None:
            if existing.request_digest != digest:
                raise _error(
                    "TARGET_IDEMPOTENCY_CONFLICT", "同一幂等键对应不同请求", 409
                )
            return _reservation(existing, subscription, replayed=True)
        binding = await self._repository.lock_binding(command.subscription_id)
        if binding is None:
            binding = await self._repository.create_binding(command.subscription_id)
        if binding.version != command.expected_version:
            raise _error("TARGET_VERSION_CONFLICT", "目标绑定版本冲突", 409)
        binding.version += 1
        run = TargetCalculationRun(
            id=uuid4(),
            subscription_id=command.subscription_id,
            subscription_version=subscription.version,
            subscription_revision_id=subscription.revision_id,
            strategy_version_id=subscription.strategy_version_id,
            idempotency_key=command.idempotency_key,
            request_digest=digest,
            parameter_snapshot=dict(subscription.parameter_snapshot),
            status="PENDING",
            failure_code=None,
            training_start_date=command.training_start_date,
            training_end_date=command.training_end_date,
            qfq_data_version=None,
            current_target_version=binding.version,
            reason=command.reason,
            resource_usage={},
            error_summary=None,
            created_at=self._now(),
        )
        binding.status = TargetStatus.CALCULATING.value
        binding.stale_reason = None
        await self._repository.persist_calculation(run)
        await self._repository.flush()
        return _reservation(run, subscription, replayed=False)

    async def apply_and_reserve(
        self, command: ApplyStrategyTargetCommand
    ) -> CalculationReservation:
        calculation = command.calculation
        changed = await self._subscriptions.switch_to_strategy(
            subscription_id=calculation.subscription_id,
            strategy_version_id=command.strategy_version_id,
            parameters=dict(command.parameter_snapshot),
            expected_version=command.expected_subscription_version,
            reason=calculation.reason,
            idempotency_key=f"{calculation.idempotency_key}:switch-strategy",
            request_id=calculation.request_id,
            actor_user_id=calculation.actor_user_id,
            session_id=calculation.session_id,
            trusted_ip=calculation.trusted_ip,
        )
        if changed is None:
            raise _error("TARGET_SUBSCRIPTION_NOT_FOUND", "监控订阅不存在", 404)
        return await self.reserve(calculation)

    async def mark_running(self, run_id: UUID, *, data_version: int) -> None:
        run = await self._required_run(run_id, lock=True)
        if run.status == "PENDING":
            run.status = "RUNNING"
            run.qfq_data_version = data_version
            await self._repository.flush()

    async def list_calculations(self, *, page: int = 1, page_size: int = 50):
        rows = await self._repository.list_calculations(page=page, page_size=page_size)
        return (
            tuple(_calculation_view(row) for row in rows),
            await self._repository.count_calculations(),
        )

    async def list_reviews(self, *, page: int = 1, page_size: int = 50):
        rows = await self._repository.list_reviews(page=page, page_size=page_size)
        return (
            tuple(_review_view(row) for row in rows),
            await self._repository.count_reviews(),
        )

    async def complete(
        self,
        run_id: UUID,
        *,
        values: TargetValues,
        target_date: date,
        source_code_hash: str,
        current_data_version: int,
        resource_usage: Mapping[str, Any] | None = None,
    ) -> CalculationResult:
        run = await self._required_run(run_id, lock=True)
        if run.status == "SUCCEEDED":
            return await self.result(run.id, replayed=True)
        if run.status == "FAILED":
            raise _error("TARGET_CALCULATION_FINISHED", "计算任务已经结束", 409)
        subscription = await self._subscriptions.lock(run.subscription_id)
        binding = await self._repository.lock_binding(run.subscription_id)
        if subscription is None or binding is None:
            return await self._fail_locked(
                run, binding, "TARGET_CALCULATION_STALE", "订阅或目标绑定已不存在"
            )
        frozen = (
            subscription.version == run.subscription_version
            and subscription.revision_id == run.subscription_revision_id
            and subscription.strategy_version_id == run.strategy_version_id
            and dict(subscription.parameter_snapshot) == dict(run.parameter_snapshot)
            and binding.version == run.current_target_version
            and run.qfq_data_version == current_data_version
        )
        if not frozen:
            return await self._fail_locked(
                run, binding, "TARGET_CALCULATION_STALE", "计算期间配置或数据已变化"
            )
        current = (
            await self._repository.get_revision(binding.current_revision_id)
            if binding.current_revision_id
            else None
        )
        await self._supersede_pending(run.subscription_id)
        if current is not None and _values(current) == values:
            run.status = "SUCCEEDED"
            run.resource_usage = dict(resource_usage or {})
            _set_result(run, "TARGET_CALCULATION_UNCHANGED", current.id, None)
            binding.status = TargetStatus.READY.value
            binding.stale_reason = None
            await self._repository.flush()
            return CalculationResult("TARGET_CALCULATION_UNCHANGED", run.id, current.id)
        revision = await self._new_strategy_revision(
            run,
            values=values,
            target_date=target_date,
            source_code_hash=source_code_hash,
        )
        run.status = "SUCCEEDED"
        run.resource_usage = dict(resource_usage or {})
        if current is not None and _large_change(
            _values(current), values, self._threshold
        ):
            changes = _changes(_values(current), values)
            review = TargetReview(
                id=uuid4(),
                candidate_revision_id=revision.id,
                baseline_revision_id=current.id,
                status="PENDING",
                reason=run.reason or "策略目标变化复核",
                **changes,
                reviewer_user_id=None,
                review_comment=None,
                reviewed_at=None,
                created_at=self._now(),
            )
            await self._repository.persist_review(review)
            _set_result(run, "TARGET_REVIEW_REQUIRED", revision.id, review.id)
            binding.status = TargetStatus.REVIEW_REQUIRED.value
            binding.stale_reason = "LARGE_TARGET_CHANGE"
            await self._emit(run, "target.review_required", revision.id)
            await self._repository.flush()
            return CalculationResult(
                "TARGET_REVIEW_REQUIRED", run.id, revision.id, review.id
            )
        await self._activate(run, binding, revision, action="target.strategy_activated")
        _set_result(run, "TARGET_CALCULATION_SUCCEEDED", revision.id, None)
        await self._repository.flush()
        return CalculationResult("TARGET_CALCULATION_SUCCEEDED", run.id, revision.id)

    async def fail(self, run_id: UUID, *, code: str, summary: str) -> CalculationResult:
        run = await self._required_run(run_id, lock=True)
        if run.status in {"SUCCEEDED", "FAILED"}:
            return await self.result(run.id, replayed=True)
        binding = await self._repository.lock_binding(run.subscription_id)
        return await self._fail_locked(run, binding, code, summary)

    async def result(self, run_id: UUID, *, replayed: bool = False):
        run = await self._required_run(run_id, lock=False)
        snapshot = dict(run.resource_usage or {})
        revision_id = _optional_uuid(snapshot.get("_result_revision_id"))
        review_id = _optional_uuid(snapshot.get("_result_review_id"))
        code = str(
            snapshot.get("_result_code")
            or (
                "TARGET_CALCULATION_FAILED"
                if run.status == "FAILED"
                else "TARGET_CALCULATION_SUCCEEDED"
            )
        )
        return CalculationResult(
            code,
            run.id,
            revision_id,
            review_id,
            replayed=replayed,
        )

    async def review_freshness(self, review_id: UUID):
        review = await self._repository.get_review(review_id)
        if review is None:
            raise _error("TARGET_REVIEW_NOT_FOUND", "复核任务不存在", 404)
        candidate = await self._repository.get_revision(review.candidate_revision_id)
        if candidate is None:
            raise _error("TARGET_REVIEW_STALE", "复核候选目标不存在", 409)
        run = await self._required_run(
            _calculation_run_id(candidate.idempotency_key), lock=False
        )
        subscription = await self._subscriptions.lock(candidate.subscription_id)
        if subscription is None:
            raise _error("TARGET_REVIEW_STALE", "订阅已经不存在", 409)
        return (
            subscription.security_id,
            run.training_start_date,
            run.training_end_date,
        )

    async def decide(
        self, command: ReviewCommand, *, approve: bool
    ) -> CalculationResult:
        review = await self._repository.get_review(command.review_id, for_update=True)
        if review is None:
            raise _error("TARGET_REVIEW_NOT_FOUND", "复核任务不存在", 404)
        replay = await self._audit.find_by_idempotency(_review_audit_key(command))
        if replay is not None:
            expected = "APPROVED" if approve else "REJECTED"
            after = dict(replay.after_summary or {})
            if after.get("decision") != expected or after.get(
                "_request_digest"
            ) != _review_digest(command, approve):
                raise _error(
                    "TARGET_IDEMPOTENCY_CONFLICT", "幂等键对应的复核决定不同", 409
                )
            return CalculationResult(
                "TARGET_REVIEW_APPROVED" if approve else "TARGET_REVIEW_REJECTED",
                UUID(int=0),
                review.candidate_revision_id,
                review.id,
                replayed=True,
            )
        if review.status != "PENDING":
            raise _error("TARGET_REVIEW_ALREADY_DECIDED", "复核任务已处理", 409)
        candidate = await self._repository.get_revision(review.candidate_revision_id)
        baseline = await self._repository.get_revision(review.baseline_revision_id)
        if candidate is None or baseline is None:
            raise _error("TARGET_REVIEW_STALE", "复核关联的目标版本已失效", 409)
        subscription = await self._subscriptions.lock(candidate.subscription_id)
        binding = await self._repository.lock_binding(candidate.subscription_id)
        valid = (
            subscription is not None
            and binding is not None
            and subscription.target_mode == "STRATEGY"
            and subscription.strategy_version_id == candidate.strategy_version_id
            and dict(subscription.parameter_snapshot)
            == dict(candidate.parameter_snapshot)
            and binding.current_revision_id == baseline.id
            and binding.version == command.expected_version
            and command.current_data_version == candidate.data_version
        )
        if not valid:
            review.status = "SUPERSEDED"
            await self._repository.flush()
            return CalculationResult(
                "TARGET_REVIEW_STALE", UUID(int=0), candidate.id, review.id
            )
        review.status = "APPROVED" if approve else "REJECTED"
        review.reviewer_user_id = command.actor_user_id
        review.review_comment = command.comment.strip()
        review.reviewed_at = self._now()
        if not review.review_comment:
            raise _error("TARGET_REVIEW_COMMENT_REQUIRED", "复核意见不能为空", 422)
        if approve:
            await self._activate(
                command, binding, candidate, action="target.review_approved"
            )
            code = "TARGET_REVIEW_APPROVED"
        else:
            binding.status = TargetStatus.STALE.value
            binding.stale_reason = "TARGET_REVIEW_REJECTED"
            await self._emit(command, "target.review_rejected", candidate.id)
            await self._audit_review(command, review, approve=False)
            await self._repository.flush()
            code = "TARGET_REVIEW_REJECTED"
        return CalculationResult(code, UUID(int=0), candidate.id, review.id)

    async def _new_strategy_revision(
        self, run, *, values, target_date, source_code_hash
    ):
        revision = TargetRevision(
            id=uuid4(),
            subscription_id=run.subscription_id,
            revision_no=await self._repository.next_revision_no(run.subscription_id),
            low_strong=values.low_strong,
            low_watch=values.low_watch,
            high_watch=values.high_watch,
            high_strong=values.high_strong,
            source=TargetSource.STRATEGY.value,
            source_revision_id=None,
            target_date=target_date,
            strategy_version_id=run.strategy_version_id,
            parameter_snapshot=dict(run.parameter_snapshot),
            data_version=run.qfq_data_version,
            source_code_hash=source_code_hash,
            content_hash=_hash(
                {
                    "subscription_id": str(run.subscription_id),
                    "values": values.model_dump(mode="json"),
                    "strategy_version_id": str(run.strategy_version_id),
                    "parameters": dict(run.parameter_snapshot),
                    "data_version": run.qfq_data_version,
                    "target_date": target_date.isoformat(),
                }
            ),
            reason=run.reason or "策略目标计算",
            large_change_confirmed=False,
            request_id=f"calculation:{run.id}",
            idempotency_key=f"calculation:{run.id}",
            actor_user_id="system",
            session_id="system",
            trusted_ip="internal",
            created_at=self._now(),
        )
        await self._repository.persist_revision(revision)
        await self._repository.flush()
        return revision

    async def _activate(self, command, binding, revision, *, action):
        before = binding.current_revision_id
        command_id = getattr(command, "id", getattr(command, "review_id", revision.id))
        request_id = getattr(command, "request_id", f"calculation:{command_id}")
        binding.current_revision_id = revision.id
        binding.status = TargetStatus.READY.value
        binding.version += 1
        binding.activated_at = self._now()
        binding.stale_reason = None
        await self._audit.append(
            AuditWrite(
                action_code=action,
                object_type="target_revision",
                object_id=str(revision.id),
                result="SUCCESS",
                request_id=request_id,
                idempotency_key=(
                    _review_audit_key(command)
                    if isinstance(command, ReviewCommand)
                    else f"{action}:{revision.id}"
                ),
                risk_level="HIGH",
                reason=getattr(command, "reason", None)
                or getattr(command, "comment", None),
                before_summary={"revision_id": str(before) if before else None},
                after_summary={
                    "revision_id": str(revision.id),
                    "binding_version": binding.version,
                    **(
                        {"decision": "APPROVED"}
                        if isinstance(command, ReviewCommand)
                        else {}
                    ),
                    **(
                        {"_request_digest": _review_digest(command, True)}
                        if isinstance(command, ReviewCommand)
                        else {}
                    ),
                },
                actor_user_id=getattr(command, "actor_user_id", "system"),
                session_id=getattr(command, "session_id", "system"),
                trusted_ip=getattr(command, "trusted_ip", "internal"),
            )
        )
        await self._emit(command, "target.activated", revision.id)
        await self._emit(command, "signal.reevaluation_requested", revision.id)
        await self._repository.flush()

    async def _fail_locked(self, run, binding, code, summary):
        if run.status in {"SUCCEEDED", "FAILED"}:
            return await self.result(run.id, replayed=True)
        run.status = "FAILED"
        run.failure_code = "TARGET_CALCULATION_FAILED"
        run.error_summary = f"{code}: {summary}"[:500]
        _set_result(run, "TARGET_CALCULATION_FAILED", None, None)
        if binding is not None and binding.version == run.current_target_version:
            binding.status = (
                TargetStatus.STALE.value
                if binding.current_revision_id
                else TargetStatus.FAILED.value
            )
            binding.stale_reason = code
        await self._emit(run, "target.calculation_failed", None)
        await self._repository.flush()
        return CalculationResult("TARGET_CALCULATION_FAILED", run.id)

    async def _supersede_pending(self, subscription_id):
        reviews = await self._repository.list_pending_reviews_for_subscription(
            subscription_id
        )
        for review in reviews:
            review.status = "SUPERSEDED"

    async def _audit_review(self, command, review, *, approve):
        await self._audit.append(
            AuditWrite(
                action_code=(
                    "target.review_approved" if approve else "target.review_rejected"
                ),
                object_type="target_review",
                object_id=str(review.id),
                result="SUCCESS",
                request_id=command.request_id,
                idempotency_key=_review_audit_key(command),
                risk_level="HIGH",
                reason=command.comment,
                before_summary={"status": "PENDING"},
                after_summary={
                    "status": review.status,
                    "decision": "APPROVED" if approve else "REJECTED",
                    "_request_digest": _review_digest(command, approve),
                },
                actor_user_id=command.actor_user_id,
                session_id=command.session_id,
                trusted_ip=command.trusted_ip,
            )
        )

    async def _required_run(self, run_id, *, lock):
        run = await self._repository.get_calculation(run_id, for_update=lock)
        if run is None:
            raise _error("TARGET_CALCULATION_NOT_FOUND", "目标计算任务不存在", 404)
        return run

    async def _emit(self, command, event_type, revision_id):
        subscription_id = (
            command.subscription_id if hasattr(command, "subscription_id") else None
        )
        if subscription_id is None and revision_id is not None:
            revision = await self._repository.get_revision(revision_id)
            subscription_id = revision.subscription_id
        event_id = getattr(command, "id", getattr(command, "review_id", revision_id))
        await self._events.append(
            TargetEvent(
                event_type=event_type,
                aggregate_id=str(subscription_id),
                dedupe_key=f"{event_type}:{event_id}",
                payload={
                    "subscription_id": str(subscription_id),
                    "revision_id": str(revision_id) if revision_id else None,
                },
            )
        )


def _reservation(run, subscription, *, replayed):
    return CalculationReservation(
        run.id,
        replayed,
        run.subscription_id,
        subscription.security_id,
        subscription.symbol,
        run.subscription_version,
        run.subscription_revision_id,
        run.strategy_version_id,
        dict(run.parameter_snapshot),
        run.status,
    )


def _values(row):
    return TargetValues(
        low_strong=row.low_strong,
        low_watch=row.low_watch,
        high_watch=row.high_watch,
        high_strong=row.high_strong,
    )


def _relative(before, after):
    return abs(after - before) / max(abs(before), Decimal("0.01"))


def _large_change(before, after, threshold):
    return any(value > threshold for value in _change_values(before, after))


def _change_values(before, after):
    return tuple(
        _relative(a, b)
        for a, b in zip(
            before.model_dump().values(), after.model_dump().values(), strict=True
        )
    )


def _changes(before, after):
    values = _change_values(before, after)
    return dict(
        zip(
            (
                "low_strong_change",
                "low_watch_change",
                "high_watch_change",
                "high_strong_change",
            ),
            values,
            strict=True,
        )
    )


def _digest(command):
    return _hash(
        {
            "subscription_id": str(command.subscription_id),
            "target_date": command.target_date.isoformat(),
            "training_start_date": command.training_start_date.isoformat(),
            "training_end_date": command.training_end_date.isoformat(),
            "reason": command.reason,
            "expected_version": command.expected_version,
            "actor_user_id": command.actor_user_id,
        }
    )


def _hash(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _error(code, message, status):
    return AppError(code=code, message=message, status_code=status)


def _calculation_run_id(idempotency_key: str) -> UUID:
    prefix = "calculation:"
    if not idempotency_key.startswith(prefix):
        raise _error("TARGET_REVIEW_STALE", "复核计算来源无效", 409)
    try:
        return UUID(idempotency_key.removeprefix(prefix))
    except ValueError as exc:
        raise _error("TARGET_REVIEW_STALE", "复核计算来源无效", 409) from exc


def _review_audit_key(command: ReviewCommand) -> str:
    digest = hashlib.sha256(command.idempotency_key.encode()).hexdigest()
    return f"target-review:{command.review_id}:{digest}"


def _review_digest(command: ReviewCommand, approve: bool) -> str:
    return _hash(
        {
            "review_id": str(command.review_id),
            "decision": "APPROVED" if approve else "REJECTED",
            "comment": command.comment,
            "expected_version": command.expected_version,
            "actor_user_id": command.actor_user_id,
        }
    )


def _set_result(run, code: str, revision_id: UUID | None, review_id: UUID | None):
    usage = dict(run.resource_usage or {})
    usage.update(
        {
            "_result_code": code,
            "_result_revision_id": str(revision_id) if revision_id else None,
            "_result_review_id": str(review_id) if review_id else None,
        }
    )
    run.resource_usage = usage


def _optional_uuid(value) -> UUID | None:
    return UUID(str(value)) if value else None


def _calculation_view(row):
    return TargetCalculationRunView(
        id=row.id,
        subscription_id=row.subscription_id,
        subscription_version=row.subscription_version,
        subscription_revision_id=row.subscription_revision_id,
        strategy_version_id=row.strategy_version_id,
        idempotency_key=row.idempotency_key,
        request_digest=row.request_digest,
        parameter_snapshot=row.parameter_snapshot,
        status=TargetCalculationStatus(row.status),
        failure_code=(
            TargetCalculationErrorCode(row.failure_code) if row.failure_code else None
        ),
        training_start_date=row.training_start_date,
        training_end_date=row.training_end_date,
        qfq_data_version=row.qfq_data_version,
        current_target_version=row.current_target_version,
        reason=row.reason,
        resource_usage=row.resource_usage,
        error_summary=row.error_summary,
        created_at=row.created_at,
    )


def _review_view(row):
    return TargetReviewView(
        id=row.id,
        candidate_revision_id=row.candidate_revision_id,
        baseline_revision_id=row.baseline_revision_id,
        status=TargetReviewStatus(row.status),
        reason=row.reason,
        low_strong_change=row.low_strong_change,
        low_watch_change=row.low_watch_change,
        high_watch_change=row.high_watch_change,
        high_strong_change=row.high_strong_change,
        reviewer_user_id=row.reviewer_user_id,
        review_comment=row.review_comment,
        reviewed_at=row.reviewed_at,
        created_at=row.created_at,
    )
