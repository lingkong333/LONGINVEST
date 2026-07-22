from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.monitoring.application import (
    transactional_monitor_subscription_port,
)
from long_invest.modules.quotes.application import TransactionalQuoteSignalPort
from long_invest.modules.securities.application import TransactionalSignalSecurityPort
from long_invest.modules.signals.contracts import (
    EvaluationOutcome,
    EvaluationReason,
    SignalInput,
)
from long_invest.modules.signals.integrations import (
    TransactionalPositionPort,
    transactional_signal_notification_publisher,
)
from long_invest.modules.signals.outbox import SignalOutbox
from long_invest.modules.signals.repository import SignalRepository
from long_invest.modules.signals.service import SignalService
from long_invest.modules.targets.application import TransactionalTargetSnapshotPort
from long_invest.platform.database.engine import Database
from long_invest.platform.errors import AppError


@dataclass(frozen=True, slots=True)
class SignalJobItemResult:
    item_id: UUID | None
    subscription_id: UUID | None
    success: bool
    code: str


@dataclass(frozen=True, slots=True)
class SignalJobReport:
    items: tuple[SignalJobItemResult, ...]

    @property
    def succeeded(self) -> int:
        return sum(item.success for item in self.items)

    @property
    def failed(self) -> int:
        return len(self.items) - self.succeeded


class SignalJobApplication:
    """Builds frozen signal inputs and isolates every stock transaction."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def evaluate_batch(
        self,
        *,
        cycle_id: UUID,
        item_ids: tuple[UUID, ...],
        request_id: str,
    ) -> SignalJobReport:
        results = []
        for item_id in item_ids:
            try:
                outcome = await self._evaluate_quote_item(
                    cycle_id=cycle_id,
                    item_id=item_id,
                    request_id=request_id,
                )
                results.append(
                    SignalJobItemResult(
                        item_id=item_id,
                        subscription_id=outcome.evaluation.subscription_id,
                        success=True,
                        code=outcome.code,
                    )
                )
            except AppError as exc:
                results.append(
                    SignalJobItemResult(
                        item_id=item_id,
                        subscription_id=None,
                        success=False,
                        code=exc.code,
                    )
                )
            except (SQLAlchemyError, TimeoutError, OSError):
                results.append(
                    SignalJobItemResult(
                        item_id=item_id,
                        subscription_id=None,
                        success=False,
                        code="SIGNAL_ITEM_BACKEND_UNAVAILABLE",
                    )
                )
        return SignalJobReport(tuple(results))

    async def reevaluate(
        self,
        *,
        config: dict[str, Any],
        request_id: str,
        idempotency_key: str,
    ) -> EvaluationOutcome:
        async with self._database.transaction() as session:
            subscriptions = transactional_monitor_subscription_port(session)
            subscription_id = _optional_uuid(config, "subscription_id")
            if subscription_id is not None:
                subscription = await subscriptions.get_subscription_snapshot(
                    subscription_id
                )
            else:
                symbol = _required_text(config, "symbol")
                subscription = await subscriptions.get_subscription_snapshot_by_symbol(
                    symbol
                )
            if subscription is None:
                raise _error("SIGNAL_SUBSCRIPTION_NOT_FOUND", 404)

            repository = SignalRepository(session)
            state = await repository.get_state(subscription.subscription_id)
            if state is None:
                raise _error("SIGNAL_STATE_NOT_FOUND", 404)
            expected_state_version = _optional_positive_int(
                config, "expected_state_version"
            )
            if (
                expected_state_version is not None
                and state.version != expected_state_version
            ):
                raise _error("SIGNAL_INPUT_SUPERSEDED", 409)
            if state.last_price is None or state.last_price_at is None:
                raise _error("SIGNAL_PRICE_UNAVAILABLE", 409)

            targets = TransactionalTargetSnapshotPort(session)
            target = await targets.get_target_snapshot(subscription.subscription_id)
            if target is None:
                raise _error("SIGNAL_TARGET_UNAVAILABLE", 409)
            expected_target_version = _optional_positive_int(
                config, "target_binding_version"
            )
            expected_target_id = _optional_uuid(config, "target_revision_id")
            if (
                expected_target_version is not None
                and target.binding_version != expected_target_version
            ) or (
                expected_target_id is not None
                and target.revision_id != expected_target_id
            ):
                raise _error("SIGNAL_INPUT_SUPERSEDED", 409)

            securities = TransactionalSignalSecurityPort(session)
            security = await securities.get_signal_security(subscription.symbol)
            if security is None or security.security_id != subscription.security_id:
                raise _error("SIGNAL_SECURITY_NOT_FOUND", 404)
            positions = TransactionalPositionPort(session)
            position = await positions.get_position_snapshot(subscription.security_id)
            position_version = position.version if position is not None else 0
            expected_position_version = _optional_positive_int(
                config, "position_version", allow_zero=True
            )
            if (
                expected_position_version is not None
                and position_version != expected_position_version
            ):
                raise _error("SIGNAL_INPUT_SUPERSEDED", 409)

            reason = EvaluationReason(_required_text(config, "reason"))
            command = SignalInput(
                subscription_id=subscription.subscription_id,
                security_id=subscription.security_id,
                symbol=subscription.symbol,
                security_name=security.name,
                subscription_version=subscription.version,
                price=state.last_price,
                price_at=state.last_price_at,
                price_version=(state.last_price_version or 0) + 1,
                target_revision_id=target.revision_id,
                target_version=target.binding_version,
                target_date=target.target_date,
                targets=target.values,
                position_version=position_version,
                hysteresis_ratio=subscription.hysteresis_ratio,
                hysteresis_min=subscription.hysteresis_min,
                reason=reason,
                idempotency_key=idempotency_key,
                request_id=request_id,
            )
            return await _service(session).evaluate(command)

    async def _evaluate_quote_item(
        self,
        *,
        cycle_id: UUID,
        item_id: UUID,
        request_id: str,
    ) -> EvaluationOutcome:
        async with self._database.transaction() as session:
            quotes = TransactionalQuoteSignalPort(session)
            quote = await quotes.get_quote_snapshot(item_id=item_id, cycle_id=cycle_id)
            if quote is None or quote.price is None or quote.quote_time is None:
                raise _error("SIGNAL_QUOTE_INELIGIBLE", 409)
            subscriptions = transactional_monitor_subscription_port(session)
            subscription = await subscriptions.get_subscription_snapshot_by_symbol(
                quote.symbol
            )
            if subscription is None:
                raise _error("SIGNAL_SUBSCRIPTION_NOT_FOUND", 404)
            target = await TransactionalTargetSnapshotPort(session).get_target_snapshot(
                subscription.subscription_id
            )
            if target is None:
                raise _error("SIGNAL_TARGET_UNAVAILABLE", 409)
            security = await TransactionalSignalSecurityPort(
                session
            ).get_signal_security(quote.symbol)
            if security is None or security.security_id != subscription.security_id:
                raise _error("SIGNAL_SECURITY_NOT_FOUND", 404)
            position = await TransactionalPositionPort(session).get_position_snapshot(
                subscription.security_id
            )
            position_version = position.version if position is not None else 0
            price_version = max(1, int(quote.scheduled_at.timestamp() * 1_000_000))
            command = SignalInput(
                subscription_id=subscription.subscription_id,
                security_id=subscription.security_id,
                symbol=subscription.symbol,
                security_name=security.name,
                subscription_version=(
                    quote.expected_subscription_version or subscription.version
                ),
                price=quote.price,
                price_at=quote.quote_time,
                quote_scheduled_at=quote.scheduled_at,
                price_version=price_version,
                target_revision_id=target.revision_id,
                target_version=target.binding_version,
                target_date=target.target_date,
                targets=target.values,
                quote_cycle_id=cycle_id,
                quote_item_id=item_id,
                position_version=position_version,
                hysteresis_ratio=subscription.hysteresis_ratio,
                hysteresis_min=subscription.hysteresis_min,
                reason=EvaluationReason.SCHEDULED_QUOTE,
                idempotency_key=(
                    f"quote:{subscription.subscription_id}:{item_id}:"
                    f"{target.binding_version}"
                ),
                request_id=request_id,
                quote_eligible=quote.eligible_for_evaluation,
                quote_ineligibility_code=(
                    None if quote.eligible_for_evaluation else quote.status.value
                ),
            )
            return await _service(session).evaluate(command)


def _service(session: Any) -> SignalService:
    return SignalService(
        SignalRepository(session),
        subscriptions=transactional_monitor_subscription_port(session),
        targets=TransactionalTargetSnapshotPort(session),
        quotes=TransactionalQuoteSignalPort(session),
        positions=TransactionalPositionPort(session),
        notifications=transactional_signal_notification_publisher(session),
        events=SignalOutbox(session),
    )


def _required_text(config: dict[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _error("SIGNAL_JOB_CONFIG_INVALID", 422)
    return value


def _optional_uuid(config: dict[str, Any], key: str) -> UUID | None:
    value = config.get(key)
    if value is None:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise _error("SIGNAL_JOB_CONFIG_INVALID", 422) from exc


def _optional_positive_int(
    config: dict[str, Any], key: str, *, allow_zero: bool = False
) -> int | None:
    value = config.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise _error("SIGNAL_JOB_CONFIG_INVALID", 422)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise _error("SIGNAL_JOB_CONFIG_INVALID", 422) from exc
    if parsed < (0 if allow_zero else 1):
        raise _error("SIGNAL_JOB_CONFIG_INVALID", 422)
    return parsed


def _error(code: str, status_code: int) -> AppError:
    return AppError(
        code=code, message="信号任务无法处理当前输入", status_code=status_code
    )
