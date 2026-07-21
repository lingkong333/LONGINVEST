from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from long_invest.modules.monitoring.contracts import SubscriptionStatus
from long_invest.modules.positions.contracts import PositionStatus
from long_invest.modules.quotes.contracts import QuoteItemStatus
from long_invest.modules.signals.contracts import (
    EvaluationOutcome,
    EvaluationReason,
    EvaluationResult,
    NotificationClass,
    SignalEvaluationView,
    SignalEventView,
    SignalInput,
    SignalNotificationRequest,
    SignalReevaluationCommand,
    SignalReevaluationResult,
    SignalStateMutationResult,
    SignalStateResetCommand,
    SignalStateView,
    SignalZone,
)
from long_invest.modules.signals.models import SignalEvaluation, SignalEvent
from long_invest.modules.signals.outbox import SignalDomainEvent
from long_invest.modules.signals.state_machine import (
    next_zone,
    notification_class,
    should_create_event,
)
from long_invest.modules.targets.contracts import TargetStatus
from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import SubmitJob


class SignalService:
    def __init__(
        self,
        repository,
        *,
        subscriptions,
        targets,
        quotes,
        positions,
        notifications,
        audit=None,
        events=None,
        jobs=None,
    ):
        self._repository = repository
        self._subscriptions = subscriptions
        self._targets = targets
        self._quotes = quotes
        self._positions = positions
        self._notifications = notifications
        self._audit = audit
        self._events = events
        self._jobs = jobs

    async def list_states(self, *, page: int = 1, page_size: int = 50):
        rows = await self._repository.list_states(page=page, page_size=page_size)
        return (
            tuple(_state_view(row) for row in rows),
            await self._repository.count_states(),
        )

    async def list_evaluations(self, *, page: int = 1, page_size: int = 50):
        rows = await self._repository.list_evaluations(page=page, page_size=page_size)
        return (
            tuple(_evaluation_view(row) for row in rows),
            await self._repository.count_evaluations(),
        )

    async def list_events(self, *, page: int = 1, page_size: int = 50):
        rows = await self._repository.list_events(page=page, page_size=page_size)
        return (
            tuple(_event_view(row) for row in rows),
            await self._repository.count_events(),
        )

    async def get_state(self, subscription_id):
        row = await self._repository.get_state(subscription_id)
        return _state_view(row) if row is not None else None

    async def get_evaluation(self, evaluation_id):
        row = await self._repository.get_evaluation(evaluation_id)
        return _evaluation_view(row) if row is not None else None

    async def get_event(self, event_id):
        row = await self._repository.get_event(event_id)
        return _event_view(row) if row is not None else None

    async def reset(
        self, command: SignalStateResetCommand
    ) -> SignalStateMutationResult:
        self._require_mutation_ports()
        replay = await self._mutation_replay(command, reset=True)
        if replay is not None:
            return replay
        state = await self._repository.lock_state(command.subscription_id)
        if state is None:
            raise _error("SIGNAL_STATE_NOT_FOUND", "信号状态不存在", 404)
        replay = await self._mutation_replay(command, reset=True)
        if replay is not None:
            return replay
        if state.version != command.expected_version:
            raise _error(
                "SIGNAL_STATE_VERSION_CONFLICT",
                "信号状态版本已经变化",
                409,
            )

        before_zone = state.zone
        state.zone = SignalZone.UNKNOWN.value
        state.version += 1
        state.updated_at = datetime.now(UTC)
        job = await self._submit_reevaluation(
            command,
            reason=EvaluationReason.STATE_RESET,
            state_version=state.version,
            scope="signal-state-reset",
        )
        view = _state_view(state)
        await self._record_mutation(
            command,
            action_code="SIGNAL_STATE_RESET",
            event_type="signal.state_reset",
            before={"zone": before_zone, "version": command.expected_version},
            after={"state": view.model_dump(mode="json")},
            job_id=job.id,
        )
        await self._repository.flush()
        return SignalStateMutationResult(
            code="SIGNAL_STATE_RESET",
            subscription_id=command.subscription_id,
            state=view,
            reevaluation_job_id=job.id,
        )

    async def reevaluate(
        self, command: SignalReevaluationCommand
    ) -> SignalReevaluationResult:
        self._require_mutation_ports()
        replay = await self._mutation_replay(command, reset=False)
        if replay is not None:
            return replay
        state = await self._repository.lock_state(command.subscription_id)
        if state is None:
            raise _error("SIGNAL_STATE_NOT_FOUND", "信号状态不存在", 404)
        replay = await self._mutation_replay(command, reset=False)
        if replay is not None:
            return replay
        if state.version != command.expected_version:
            raise _error(
                "SIGNAL_STATE_VERSION_CONFLICT",
                "信号状态版本已经变化",
                409,
            )
        job = await self._submit_reevaluation(
            command,
            reason=EvaluationReason.MANUAL_CHECK,
            state_version=state.version,
            scope="signal-manual-reevaluation",
        )
        await self._record_mutation(
            command,
            action_code="SIGNAL_REEVALUATION_REQUESTED",
            event_type="signal.evaluation_requested",
            before={"zone": state.zone, "version": state.version},
            after={"accepted": True},
            job_id=job.id,
        )
        await self._repository.flush()
        return SignalReevaluationResult(
            code="SIGNAL_REEVALUATION_REQUESTED",
            subscription_id=command.subscription_id,
            reevaluation_job_id=job.id,
            accepted=True,
        )

    def _require_mutation_ports(self) -> None:
        if self._audit is None or self._events is None or self._jobs is None:
            raise RuntimeError("signal mutation ports are not configured")

    async def _submit_reevaluation(
        self,
        command: SignalStateResetCommand | SignalReevaluationCommand,
        *,
        reason: EvaluationReason,
        state_version: int,
        scope: str,
    ):
        return await self._jobs.submit(
            SubmitJob(
                job_type="SIGNAL_REEVALUATE",
                queue="signals",
                idempotency_scope=f"{scope}:{command.subscription_id}",
                idempotency_key=command.idempotency_key,
                request_id=command.request_id,
                config_snapshot={
                    "subscription_id": str(command.subscription_id),
                    "reason": reason.value,
                    "expected_state_version": state_version,
                    "request_id": command.request_id,
                },
                business_object_type="monitor_subscription",
                business_object_id=str(command.subscription_id),
                created_by_user_id=command.actor_user_id,
                soft_timeout_seconds=30,
                hard_timeout_seconds=60,
            )
        )

    async def _record_mutation(
        self,
        command: SignalStateResetCommand | SignalReevaluationCommand,
        *,
        action_code: str,
        event_type: str,
        before: dict[str, object],
        after: dict[str, object],
        job_id,
    ) -> None:
        digest = _action_digest(command)
        audit_after = {
            **after,
            "reevaluation_job_id": str(job_id),
            "_request_digest": digest,
        }
        await self._audit.append(
            AuditWrite(
                action_code=action_code,
                object_type="signal_state",
                object_id=str(command.subscription_id),
                result="SUCCESS",
                request_id=command.request_id,
                idempotency_key=_audit_key(
                    command.subscription_id,
                    command.idempotency_key,
                ),
                risk_level="HIGH",
                reason=command.reason,
                before_summary=before,
                after_summary=audit_after,
                actor_user_id=command.actor_user_id,
                session_id=command.session_id,
                trusted_ip=command.trusted_ip,
            )
        )
        await self._events.append(
            SignalDomainEvent(
                event_type=event_type,
                aggregate_id=str(command.subscription_id),
                dedupe_key=(
                    f"{event_type}:{command.subscription_id}:{command.idempotency_key}"
                ),
                payload={
                    "subscription_id": str(command.subscription_id),
                    "reevaluation_job_id": str(job_id),
                    "reason": command.reason,
                    "request_id": command.request_id,
                },
            )
        )

    async def _mutation_replay(
        self,
        command: SignalStateResetCommand | SignalReevaluationCommand,
        *,
        reset: bool,
    ) -> SignalStateMutationResult | SignalReevaluationResult | None:
        record = await self._audit.find_by_idempotency(
            _audit_key(command.subscription_id, command.idempotency_key)
        )
        if record is None:
            return None
        after = dict(record.after_summary or {})
        if after.get("_request_digest") != _action_digest(command):
            raise _error(
                "SIGNAL_IDEMPOTENCY_CONFLICT",
                "同一幂等键已经用于不同的信号操作",
                409,
            )
        try:
            job_id = UUID(str(after["reevaluation_job_id"]))
            if reset:
                return SignalStateMutationResult(
                    code="SIGNAL_STATE_RESET",
                    subscription_id=command.subscription_id,
                    state=SignalStateView.model_validate(after["state"]),
                    reevaluation_job_id=job_id,
                    replayed=True,
                )
            return SignalReevaluationResult(
                code="SIGNAL_REEVALUATION_REQUESTED",
                subscription_id=command.subscription_id,
                reevaluation_job_id=job_id,
                accepted=True,
                replayed=True,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _error(
                "SIGNAL_IDEMPOTENCY_CONFLICT",
                "幂等审计事实不完整",
                409,
            ) from exc

    async def evaluate(self, command: SignalInput) -> EvaluationOutcome:
        digest = _content_hash(command)
        existing = await self._repository.find_evaluation_by_idempotency(
            command.subscription_id, command.idempotency_key
        )
        if existing is not None:
            return _replay_outcome(existing, digest)

        state = await self._repository.lock_or_create_state(command.subscription_id)
        existing = await self._repository.find_evaluation_by_idempotency(
            command.subscription_id, command.idempotency_key
        )
        if existing is not None:
            return _replay_outcome(existing, digest)
        before = SignalZone(state.zone)
        subscription = await self._subscriptions.get_subscription_snapshot(
            command.subscription_id
        )
        if (
            subscription is None
            or subscription.status is not SubscriptionStatus.ENABLED
        ):
            return await self._record_without_state(
                command,
                state,
                digest,
                EvaluationResult.SKIPPED,
                "SIGNAL_SUBSCRIPTION_DISABLED",
            )
        if command.subscription_version != subscription.version:
            return await self._record_without_state(
                command,
                state,
                digest,
                EvaluationResult.SUPERSEDED,
                "SIGNAL_INPUT_SUPERSEDED",
            )
        if (
            command.security_id != subscription.security_id
            or command.symbol != subscription.symbol
            or command.hysteresis_ratio != subscription.hysteresis_ratio
            or command.hysteresis_min != subscription.hysteresis_min
        ):
            return await self._record_without_state(
                command,
                state,
                digest,
                EvaluationResult.SUPERSEDED,
                "SIGNAL_INPUT_SUPERSEDED",
            )

        target = await self._targets.get_target_snapshot(command.subscription_id)
        if target is None or target.status not in {
            TargetStatus.READY,
            TargetStatus.STALE,
        }:
            return await self._record_without_state(
                command,
                state,
                digest,
                EvaluationResult.SKIPPED,
                "SIGNAL_TARGET_UNAVAILABLE",
            )
        if (
            command.target_revision_id != target.revision_id
            or command.target_version != target.binding_version
            or command.target_date != target.target_date
            or command.targets != target.values
        ):
            return await self._record_without_state(
                command,
                state,
                digest,
                EvaluationResult.SUPERSEDED,
                "SIGNAL_INPUT_SUPERSEDED",
            )
        if command.quote_cycle_id is not None and command.quote_item_id is not None:
            quote = await self._quotes.get_quote_snapshot(
                item_id=command.quote_item_id,
                cycle_id=command.quote_cycle_id,
            )
            if quote is None:
                return await self._record_without_state(
                    command,
                    state,
                    digest,
                    EvaluationResult.SKIPPED,
                    "QUOTE_MISSING",
                    outcome_code="SIGNAL_QUOTE_INELIGIBLE",
                )
            if (
                not quote.eligible_for_evaluation
                or quote.status is not QuoteItemStatus.VALID
            ):
                return await self._record_without_state(
                    command,
                    state,
                    digest,
                    EvaluationResult.SKIPPED,
                    command.quote_ineligibility_code or quote.status.value,
                    outcome_code="SIGNAL_QUOTE_INELIGIBLE",
                )
            if not _quote_matches_command(quote, command):
                return await self._record_without_state(
                    command,
                    state,
                    digest,
                    EvaluationResult.SUPERSEDED,
                    "SIGNAL_INPUT_SUPERSEDED",
                )
        if not command.quote_eligible:
            return await self._record_without_state(
                command,
                state,
                digest,
                EvaluationResult.SKIPPED,
                command.quote_ineligibility_code or "SIGNAL_QUOTE_INELIGIBLE",
                outcome_code="SIGNAL_QUOTE_INELIGIBLE",
            )
        if _is_older_input(state, command):
            return await self._record_without_state(
                command,
                state,
                digest,
                EvaluationResult.SUPERSEDED,
                "SIGNAL_INPUT_SUPERSEDED",
            )

        position = await self._positions.get_position_snapshot(command.security_id)
        position_status = (
            position.status if position is not None else PositionStatus.NOT_HOLDING
        )
        position_version = position.version if position is not None else 0
        after = next_zone(before, command)
        changed = before is not after
        result = EvaluationResult.APPLIED if changed else EvaluationResult.UNCHANGED
        now = datetime.now(UTC)
        evaluation = self._new_evaluation(
            command,
            before,
            after,
            result,
            digest,
            target.status is TargetStatus.STALE,
            position_status,
            position_version,
            now,
        )
        await self._repository.add_evaluation(evaluation)

        event = None
        if should_create_event(before, after):
            event = self._new_event(
                command,
                evaluation,
                before,
                after,
                target.status is TargetStatus.STALE,
                position_status,
                position_version,
                state.version + 1,
                now,
            )
            await self._repository.add_event(event)

        if changed:
            state.zone = after.value
            state.version += 1
        state.last_price = command.price
        state.last_price_at = command.price_at
        state.last_subscription_version = command.subscription_version
        state.last_price_version = command.price_version
        state.last_quote_cycle_id = command.quote_cycle_id
        state.last_quote_scheduled_at = command.quote_scheduled_at
        state.last_quote_item_id = command.quote_item_id
        state.last_target_revision_id = command.target_revision_id
        state.last_target_version = command.target_version
        state.last_position_version = position_version
        state.last_evaluation_id = evaluation.id
        state.last_event_id = event.id if event is not None else state.last_event_id

        notification_event_id = event.id if event is not None else None
        notification_kind = (
            NotificationClass(event.notification_class) if event is not None else None
        )
        notification_eligible = (
            event.notification_eligible if event is not None else False
        )
        suppression_reason = event.suppression_reason if event is not None else None
        notification_key = f"signal-event:{event.id}" if event is not None else None
        if (
            event is None
            and command.reason is EvaluationReason.POSITION_BECAME_HOLDING
            and after in {SignalZone.HIGH, SignalZone.STRONG_HIGH}
            and position_status is PositionStatus.HOLDING
            and state.last_event_id is not None
        ):
            notification_event_id = state.last_event_id
            notification_kind = NotificationClass.HIGH
            notification_eligible = True
            notification_key = (
                f"signal-position-review:{command.subscription_id}:"
                f"{state.version}:{position_version}"
            )

        if notification_event_id is not None and notification_kind is not None:
            await self._notifications.publish(
                SignalNotificationRequest(
                    event_id=notification_event_id,
                    subscription_id=command.subscription_id,
                    security_id=command.security_id,
                    symbol=command.symbol,
                    security_name=command.security_name,
                    notification_class=notification_kind,
                    before_zone=before,
                    after_zone=after,
                    price=command.price,
                    price_at=command.price_at,
                    targets=command.targets,
                    target_revision_id=command.target_revision_id,
                    target_version=command.target_version,
                    target_date=command.target_date,
                    target_stale=target.status is TargetStatus.STALE,
                    position_status=position_status,
                    position_version=position_version,
                    reason=command.reason,
                    notification_mode=subscription.notification_mode,
                    eligible=notification_eligible,
                    suppression_reason=suppression_reason,
                    idempotency_key=notification_key,
                    request_id=command.request_id,
                )
            )
        await self._append_evaluation_facts(
            command,
            evaluation=evaluation,
            event=event,
            notification_event_id=notification_event_id,
            notification_eligible=notification_eligible,
            suppression_reason=suppression_reason,
        )
        await self._repository.flush()
        return EvaluationOutcome(
            code="SIGNAL_EVALUATED",
            result=result,
            state=_state_view(state),
            evaluation=_evaluation_view(evaluation),
            event=_event_view(event) if event is not None else None,
        )

    async def _record_without_state(
        self,
        command,
        state,
        digest,
        result,
        code,
        *,
        outcome_code=None,
    ):
        now = datetime.now(UTC)
        evaluation = self._new_evaluation(
            command,
            SignalZone(state.zone),
            SignalZone(state.zone),
            result,
            digest,
            False,
            None,
            command.position_version,
            now,
            skip_code=code,
        )
        await self._repository.add_evaluation(evaluation)
        await self._append_domain_event(
            "signal.evaluation_skipped",
            command,
            evaluation.id,
            {
                "evaluation_id": str(evaluation.id),
                "result": result.value,
                "skip_code": code,
            },
        )
        await self._repository.flush()
        return EvaluationOutcome(
            code=outcome_code or code,
            result=result,
            state=_state_view(state),
            evaluation=_evaluation_view(evaluation),
            event=None,
        )

    async def _append_evaluation_facts(
        self,
        command: SignalInput,
        *,
        evaluation: SignalEvaluation,
        event: SignalEvent | None,
        notification_event_id: UUID | None,
        notification_eligible: bool,
        suppression_reason: str | None,
    ) -> None:
        await self._append_domain_event(
            "signal.evaluation_completed",
            command,
            evaluation.id,
            {
                "evaluation_id": str(evaluation.id),
                "result": evaluation.result,
                "before_zone": evaluation.before_zone,
                "after_zone": evaluation.after_zone,
            },
        )
        if event is not None:
            await self._append_domain_event(
                "signal.transitioned",
                command,
                event.id,
                {
                    "event_id": str(event.id),
                    "evaluation_id": str(evaluation.id),
                    "before_zone": event.before_zone,
                    "after_zone": event.after_zone,
                    "state_version": event.state_version,
                },
            )
        if notification_event_id is not None:
            topic = (
                "signal.notification_requested"
                if notification_eligible
                else "signal.notification_suppressed"
            )
            await self._append_domain_event(
                topic,
                command,
                notification_event_id,
                {
                    "signal_event_id": str(notification_event_id),
                    "eligible": notification_eligible,
                    "suppression_reason": suppression_reason,
                },
            )

    async def _append_domain_event(
        self,
        topic: str,
        command: SignalInput,
        fact_id: UUID,
        payload: dict[str, object],
    ) -> None:
        if self._events is None:
            return
        await self._events.append(
            SignalDomainEvent(
                event_type=topic,
                aggregate_id=str(command.subscription_id),
                dedupe_key=f"{topic}:{fact_id}",
                payload={
                    "subscription_id": str(command.subscription_id),
                    "request_id": command.request_id,
                    **payload,
                },
            )
        )

    @staticmethod
    def _new_evaluation(
        command,
        before,
        after,
        result,
        digest,
        stale,
        position_status,
        position_version,
        now,
        skip_code=None,
    ):
        include = True
        return SignalEvaluation(
            id=uuid4(),
            subscription_id=command.subscription_id,
            idempotency_key=command.idempotency_key,
            reason=command.reason.value,
            result=result.value,
            before_zone=before.value,
            after_zone=after.value,
            subscription_version=command.subscription_version if include else None,
            target_revision_id=command.target_revision_id if include else None,
            target_version=command.target_version if include else None,
            target_date=command.target_date if include else None,
            low_strong=command.targets.low_strong if include else None,
            low_watch=command.targets.low_watch if include else None,
            high_watch=command.targets.high_watch if include else None,
            high_strong=command.targets.high_strong if include else None,
            position_status=position_status.value if position_status else None,
            position_version=position_version,
            price=command.price if include else None,
            price_at=command.price_at if include else None,
            price_version=command.price_version if include else None,
            quote_cycle_id=command.quote_cycle_id,
            quote_scheduled_at=command.quote_scheduled_at,
            quote_item_id=command.quote_item_id,
            hysteresis_applied=before not in {SignalZone.UNKNOWN, SignalZone.NORMAL},
            used_stale_target=stale,
            skip_code=skip_code,
            content_hash=digest,
            created_at=now,
        )

    @staticmethod
    def _new_event(
        command,
        evaluation,
        before,
        after,
        stale,
        position_status,
        position_version,
        state_version,
        now,
    ):
        kind = notification_class(before, after)
        assert kind is not None
        high = kind in {NotificationClass.HIGH, NotificationClass.HIGH_CLEARED}
        eligible = not high or position_status is PositionStatus.HOLDING
        return SignalEvent(
            id=uuid4(),
            subscription_id=command.subscription_id,
            evaluation_id=evaluation.id,
            before_zone=before.value,
            after_zone=after.value,
            reason=command.reason.value,
            price=command.price,
            price_at=command.price_at,
            target_revision_id=command.target_revision_id,
            target_version=command.target_version,
            target_date=command.target_date,
            low_strong=command.targets.low_strong,
            low_watch=command.targets.low_watch,
            high_watch=command.targets.high_watch,
            high_strong=command.targets.high_strong,
            position_status=position_status.value,
            position_version=position_version,
            quote_cycle_id=command.quote_cycle_id,
            quote_scheduled_at=command.quote_scheduled_at,
            quote_item_id=command.quote_item_id,
            used_stale_target=stale,
            state_version=state_version,
            notification_class=kind.value,
            notification_eligible=eligible,
            suppression_reason=None if eligible else "NOT_HOLDING",
            created_at=now,
        )


def _content_hash(command: SignalInput) -> str:
    payload = command.model_dump(
        mode="json",
        exclude={"idempotency_key", "request_id"},
    )
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _replay_outcome(existing, digest: str) -> EvaluationOutcome:
    if existing.content_hash != digest:
        raise AppError(
            code="SIGNAL_IDEMPOTENCY_CONFLICT",
            message="该幂等键已用于不同的信号判断",
            status_code=409,
        )
    return EvaluationOutcome(
        code="SIGNAL_EVALUATION_REPLAYED",
        result=EvaluationResult(existing.result),
        state=None,
        evaluation=_evaluation_view(existing),
        event=None,
        replayed=True,
    )


def _state_view(row) -> SignalStateView:
    return SignalStateView(
        subscription_id=row.subscription_id,
        zone=SignalZone(row.zone),
        version=row.version,
        last_price=row.last_price,
        last_price_at=row.last_price_at,
        last_subscription_version=row.last_subscription_version,
        last_price_version=row.last_price_version,
        last_quote_cycle_id=row.last_quote_cycle_id,
        last_quote_scheduled_at=row.last_quote_scheduled_at,
        last_quote_item_id=row.last_quote_item_id,
        last_target_revision_id=row.last_target_revision_id,
        last_target_version=row.last_target_version,
        last_position_version=row.last_position_version,
    )


def _evaluation_view(row) -> SignalEvaluationView:
    targets = None
    if row.low_strong is not None:
        from long_invest.modules.targets.contracts import TargetValues

        targets = TargetValues(
            low_strong=row.low_strong,
            low_watch=row.low_watch,
            high_watch=row.high_watch,
            high_strong=row.high_strong,
        )
    return SignalEvaluationView(
        id=row.id,
        subscription_id=row.subscription_id,
        reason=row.reason,
        result=row.result,
        before_zone=row.before_zone,
        after_zone=row.after_zone,
        subscription_version=row.subscription_version,
        target_revision_id=row.target_revision_id,
        target_version=row.target_version,
        target_date=row.target_date,
        targets=targets,
        position_status=row.position_status,
        position_version=row.position_version,
        price=row.price,
        price_at=row.price_at,
        price_version=row.price_version,
        quote_cycle_id=row.quote_cycle_id,
        quote_scheduled_at=row.quote_scheduled_at,
        quote_item_id=row.quote_item_id,
        hysteresis_applied=row.hysteresis_applied,
        used_stale_target=row.used_stale_target,
        skip_code=row.skip_code,
        content_hash=row.content_hash,
        created_at=row.created_at,
    )


def _event_view(row) -> SignalEventView:
    from long_invest.modules.targets.contracts import TargetValues

    return SignalEventView(
        id=row.id,
        subscription_id=row.subscription_id,
        evaluation_id=row.evaluation_id,
        before_zone=row.before_zone,
        after_zone=row.after_zone,
        reason=row.reason,
        price=row.price,
        price_at=row.price_at,
        targets=TargetValues(
            low_strong=row.low_strong,
            low_watch=row.low_watch,
            high_watch=row.high_watch,
            high_strong=row.high_strong,
        ),
        target_revision_id=row.target_revision_id,
        target_version=row.target_version,
        target_date=row.target_date,
        position_status=row.position_status,
        position_version=row.position_version,
        quote_cycle_id=row.quote_cycle_id,
        quote_scheduled_at=row.quote_scheduled_at,
        quote_item_id=row.quote_item_id,
        used_stale_target=row.used_stale_target,
        state_version=row.state_version,
        notification_class=row.notification_class,
        notification_eligible=row.notification_eligible,
        suppression_reason=row.suppression_reason,
        created_at=row.created_at,
    )


def _is_older_input(state, command: SignalInput) -> bool:
    target_reclassification = command.reason is EvaluationReason.TARGET_ACTIVATED and (
        state.last_target_version is None
        or command.target_version > state.last_target_version
    )
    if target_reclassification:
        return False
    if state.last_price_at is not None and command.price_at < state.last_price_at:
        return True
    if (
        state.last_quote_scheduled_at is not None
        and command.quote_scheduled_at is not None
        and command.quote_scheduled_at < state.last_quote_scheduled_at
    ):
        return True
    return (
        state.last_price_version is not None
        and command.price_version <= state.last_price_version
    )


def _quote_matches_command(quote, command: SignalInput) -> bool:
    expected_subscription_version = quote.expected_subscription_version
    return (
        quote.cycle_id == command.quote_cycle_id
        and quote.item_id == command.quote_item_id
        and quote.symbol == command.symbol
        and quote.price == command.price
        and quote.quote_time == command.price_at
        and quote.scheduled_at == command.quote_scheduled_at
        and (
            expected_subscription_version is None
            or expected_subscription_version == command.subscription_version
        )
    )


def _action_digest(
    command: SignalStateResetCommand | SignalReevaluationCommand,
) -> str:
    payload = command.model_dump(mode="json", exclude={"idempotency_key", "request_id"})
    payload["operation"] = type(command).__name__
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _audit_key(subscription_id: UUID, key: str) -> str:
    return f"signal:{subscription_id}:" + hashlib.sha256(key.encode()).hexdigest()


def _error(code: str, message: str, status_code: int) -> AppError:
    return AppError(code=code, message=message, status_code=status_code)
