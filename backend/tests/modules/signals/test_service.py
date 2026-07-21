from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.modules.monitoring.contracts import (
    SubscriptionSignalSnapshot,
    SubscriptionStatus,
)
from long_invest.modules.positions.contracts import PositionStatus, PositionView
from long_invest.modules.quotes.contracts import (
    QuoteItemStatus,
    SignalQuoteSnapshot,
)
from long_invest.modules.signals.contracts import (
    EvaluationReason,
    EvaluationResult,
    SignalInput,
    SignalReevaluationCommand,
    SignalStateResetCommand,
    SignalZone,
)
from long_invest.modules.signals.models import SignalState
from long_invest.modules.signals.service import SignalService
from long_invest.modules.targets.contracts import (
    TargetSnapshot,
    TargetSource,
    TargetStatus,
    TargetValues,
)
from long_invest.platform.errors import AppError

NOW = datetime(2026, 7, 17, 9, 30, tzinfo=UTC)


class Repository:
    def __init__(self, zone=SignalZone.UNKNOWN):
        self.state = SignalState(subscription_id=uuid4(), zone=zone.value, version=1)
        self.evaluations = []
        self.events = []
        self.replays = {}
        self.hide_next_replay = False
        self.idempotency_reads = 0

    async def find_evaluation_by_idempotency(self, subscription_id, key):
        self.idempotency_reads += 1
        if self.hide_next_replay:
            self.hide_next_replay = False
            return None
        return self.replays.get((subscription_id, key))

    async def lock_or_create_state(self, subscription_id):
        self.state.subscription_id = subscription_id
        return self.state

    async def lock_state(self, subscription_id):
        if self.state is None:
            return None
        self.state.subscription_id = subscription_id
        return self.state

    async def add_evaluation(self, row):
        self.evaluations.append(row)
        self.replays[(row.subscription_id, row.idempotency_key)] = row

    async def add_event(self, row):
        self.events.append(row)

    async def flush(self):
        return None


class Audit:
    def __init__(self):
        self.records = {}

    async def find_by_idempotency(self, key):
        return self.records.get(key)

    async def append(self, record):
        self.records[record.idempotency_key] = SimpleNamespace(
            after_summary=record.after_summary
        )


class Events:
    def __init__(self):
        self.items = []

    async def append(self, event):
        self.items.append(event)


class Jobs:
    def __init__(self):
        self.items = []

    async def submit(self, command):
        self.items.append(command)
        return SimpleNamespace(id=uuid4())


class Port:
    def __init__(self, value):
        self.value = value

    async def get_subscription_snapshot(self, _id):
        return self.value

    async def get_target_snapshot(self, _id):
        return self.value

    async def get_position_snapshot(self, _id):
        return self.value


class Notifications:
    def __init__(self):
        self.items = []

    async def publish(self, item):
        self.items.append(item)


class Quotes:
    def __init__(self):
        self.value = None

    async def get_quote_snapshot(self, *, item_id, cycle_id):
        if self.value is None:
            return None
        if (item_id, cycle_id) != (self.value.item_id, self.value.cycle_id):
            return None
        return self.value


def setup_case(
    *,
    zone=SignalZone.UNKNOWN,
    status=SubscriptionStatus.ENABLED,
    target_status=TargetStatus.READY,
    holding=False,
):
    subscription_id, security_id, revision_id = uuid4(), uuid4(), uuid4()
    subscription = SubscriptionSignalSnapshot(
        subscription_id=subscription_id,
        security_id=security_id,
        symbol="600000.SH",
        status=status,
        version=3,
        revision_id=uuid4(),
        target_mode="MANUAL",
        hysteresis_ratio="0.02",
        hysteresis_min="0.02",
        notification_mode="DEFAULT",
    )
    targets = TargetValues(
        low_strong="8", low_watch="9", high_watch="11", high_strong="12"
    )
    target = TargetSnapshot(
        subscription_id=subscription_id,
        revision_id=revision_id,
        revision_no=2,
        binding_version=4,
        values=targets,
        source=TargetSource.MANUAL,
        status=target_status,
        target_date=date(2026, 7, 17),
        parameter_snapshot={},
        content_hash="a" * 64,
        activated_at=NOW,
    )
    position = PositionView(
        security_id=security_id,
        symbol="600000.SH",
        status=PositionStatus.HOLDING if holding else PositionStatus.NOT_HOLDING,
        version=5,
    )
    repo, notifications, quotes = Repository(zone), Notifications(), Quotes()
    service = SignalService(
        repo,
        subscriptions=Port(subscription),
        targets=Port(target),
        quotes=quotes,
        positions=Port(position),
        notifications=notifications,
    )
    return SimpleNamespace(
        service=service,
        repo=repo,
        notifications=notifications,
        subscription=subscription,
        target=target,
        position=position,
        quotes=quotes,
    )


def signal_input(case, price="10", **overrides):
    quote_cycle_id = uuid4()
    quote_item_id = uuid4()
    values = dict(
        subscription_id=case.subscription.subscription_id,
        security_id=case.subscription.security_id,
        symbol="600000.SH",
        security_name="浦发银行",
        subscription_version=3,
        price=price,
        price_at=NOW,
        quote_scheduled_at=NOW,
        price_version=10,
        target_revision_id=case.target.revision_id,
        target_version=4,
        target_date=case.target.target_date,
        targets=case.target.values,
        position_version=5,
        hysteresis_ratio="0.02",
        hysteresis_min="0.02",
        reason=EvaluationReason.SCHEDULED_QUOTE,
        idempotency_key="eval-1",
        request_id="request-1",
        quote_cycle_id=quote_cycle_id,
        quote_item_id=quote_item_id,
    )
    values.update(overrides)
    command = SignalInput(**values)
    if command.quote_cycle_id is not None and command.quote_item_id is not None:
        case.quotes.value = SignalQuoteSnapshot(
            cycle_id=command.quote_cycle_id,
            item_id=command.quote_item_id,
            symbol=command.symbol,
            status=(
                QuoteItemStatus.VALID
                if command.quote_eligible
                else QuoteItemStatus.CONFLICT
            ),
            price=command.price,
            quote_time=command.price_at,
            scheduled_at=command.quote_scheduled_at,
            eligible_for_evaluation=command.quote_eligible,
            expected_subscription_version=command.subscription_version,
        )
    return command


@pytest.mark.anyio
async def test_unknown_to_normal_is_silent_baseline():
    case = setup_case()
    result = await case.service.evaluate(signal_input(case))
    assert result.state.zone is SignalZone.NORMAL
    assert result.evaluation.result is EvaluationResult.APPLIED
    assert result.event is None
    assert case.notifications.items == []


@pytest.mark.anyio
async def test_high_without_position_persists_event_but_suppresses_notification():
    case = setup_case()
    result = await case.service.evaluate(signal_input(case, "11.50"))
    assert result.event.notification_eligible is False
    assert result.event.suppression_reason == "NOT_HOLDING"
    assert len(case.notifications.items) == 1
    notification = case.notifications.items[0]
    assert notification.eligible is False
    assert notification.suppression_reason == "NOT_HOLDING"


@pytest.mark.anyio
async def test_low_without_position_is_not_suppressed():
    case = setup_case()
    result = await case.service.evaluate(signal_input(case, "8.50"))
    assert result.event.notification_eligible is True
    assert len(case.notifications.items) == 1
    notification = case.notifications.items[0]
    assert notification.price_at == NOW
    assert notification.security_name == "浦发银行"
    assert notification.targets == case.target.values
    assert notification.target_version == case.target.binding_version
    assert notification.position_status is PositionStatus.NOT_HOLDING
    assert notification.reason is EvaluationReason.SCHEDULED_QUOTE
    assert notification.request_id == "request-1"


@pytest.mark.anyio
async def test_unchanged_and_direct_crossing_have_stable_event_semantics():
    unchanged = setup_case(zone=SignalZone.LOW)
    same = await unchanged.service.evaluate(signal_input(unchanged, "8.50"))
    assert same.evaluation.result is EvaluationResult.UNCHANGED
    assert same.event is None

    crossing = setup_case(zone=SignalZone.HIGH)
    changed = await crossing.service.evaluate(signal_input(crossing, "8.50"))
    assert (changed.event.before_zone, changed.event.after_zone) == (
        SignalZone.HIGH,
        SignalZone.LOW,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kind", "expected", "code"),
    [
        ("paused", EvaluationResult.SKIPPED, "SIGNAL_SUBSCRIPTION_DISABLED"),
        ("missing", EvaluationResult.SKIPPED, "SIGNAL_TARGET_UNAVAILABLE"),
        ("quote", EvaluationResult.SKIPPED, "SIGNAL_QUOTE_INELIGIBLE"),
        ("subscription", EvaluationResult.SUPERSEDED, "SIGNAL_INPUT_SUPERSEDED"),
        ("target", EvaluationResult.SUPERSEDED, "SIGNAL_INPUT_SUPERSEDED"),
        ("price", EvaluationResult.SUPERSEDED, "SIGNAL_INPUT_SUPERSEDED"),
    ],
)
async def test_invalid_or_old_inputs_only_write_evaluation(kind, expected, code):
    case = setup_case(
        status=SubscriptionStatus.PAUSED
        if kind == "paused"
        else SubscriptionStatus.ENABLED,
        target_status=TargetStatus.MISSING if kind == "missing" else TargetStatus.READY,
    )
    if kind == "price":
        case.repo.state.last_price_version = 10
    overrides = {}
    if kind == "quote":
        overrides = {"quote_eligible": False, "quote_ineligibility_code": "CONFLICT"}
    elif kind == "subscription":
        overrides = {"subscription_version": 2}
    elif kind == "target":
        overrides = {"target_version": 3}
    result = await case.service.evaluate(signal_input(case, **overrides))
    assert result.evaluation.result is expected
    assert result.evaluation.skip_code == ("CONFLICT" if kind == "quote" else code)
    assert case.repo.state.zone == SignalZone.UNKNOWN.value
    assert case.repo.events == []
    assert result.evaluation.price == Decimal("10.000000")
    assert result.evaluation.targets == case.target.values
    if expected is EvaluationResult.SUPERSEDED:
        assert result.evaluation.position_status is None


@pytest.mark.anyio
async def test_stale_target_is_evaluated_and_marked():
    case = setup_case(target_status=TargetStatus.STALE)
    result = await case.service.evaluate(signal_input(case, "8.50"))
    assert result.evaluation.used_stale_target is True
    assert result.event.used_stale_target is True


@pytest.mark.anyio
async def test_same_idempotency_replays_and_different_content_conflicts():
    case = setup_case()
    command = signal_input(case, "8.50")
    original = await case.service.evaluate(command)
    replay = await case.service.evaluate(command)
    assert replay.replayed is True
    assert replay.evaluation.id == original.evaluation.id
    with pytest.raises(AppError) as exc:
        await case.service.evaluate(
            command.model_copy(update={"price": Decimal("8.40")})
        )
    assert exc.value.code == "SIGNAL_IDEMPOTENCY_CONFLICT"


@pytest.mark.anyio
async def test_request_id_does_not_change_idempotent_business_content():
    case = setup_case()
    command = signal_input(case, "8.50")
    original = await case.service.evaluate(command)

    replay = await case.service.evaluate(
        command.model_copy(update={"request_id": "retry-request-2"})
    )

    assert replay.replayed is True
    assert replay.evaluation.id == original.evaluation.id


@pytest.mark.anyio
async def test_idempotency_is_rechecked_after_state_lock_for_concurrent_replay():
    case = setup_case()
    command = signal_input(case, "8.50")
    original = await case.service.evaluate(command)
    case.repo.hide_next_replay = True

    replay = await case.service.evaluate(command)

    assert replay.replayed is True
    assert replay.evaluation.id == original.evaluation.id
    assert len(case.repo.evaluations) == 1


@pytest.mark.anyio
async def test_position_became_holding_reviews_high_without_new_transition():
    case = setup_case(zone=SignalZone.HIGH, holding=True)
    existing_event_id = uuid4()
    case.repo.state.last_event_id = existing_event_id

    result = await case.service.evaluate(
        signal_input(
            case,
            "11.50",
            reason=EvaluationReason.POSITION_BECAME_HOLDING,
            idempotency_key="position-review-1",
        )
    )

    assert result.evaluation.result is EvaluationResult.UNCHANGED
    assert result.event is None
    assert case.repo.events == []
    assert len(case.notifications.items) == 1
    notification = case.notifications.items[0]
    assert notification.event_id == existing_event_id
    assert notification.eligible is True
    assert notification.position_version == case.position.version


@pytest.mark.anyio
@pytest.mark.parametrize(
    "overrides",
    [
        {"security_id": uuid4()},
        {"symbol": "000001.SZ"},
        {"hysteresis_ratio": Decimal("0.03")},
        {"hysteresis_min": Decimal("0.03")},
        {
            "targets": TargetValues(
                low_strong="7", low_watch="8", high_watch="11", high_strong="12"
            )
        },
    ],
)
async def test_frozen_input_must_match_current_subscription_and_target(overrides):
    case = setup_case()
    result = await case.service.evaluate(signal_input(case, **overrides))
    assert result.evaluation.result is EvaluationResult.SUPERSEDED
    assert result.evaluation.skip_code == "SIGNAL_INPUT_SUPERSEDED"
    assert case.repo.state.zone == SignalZone.UNKNOWN.value


@pytest.mark.anyio
async def test_older_quote_time_is_superseded_even_with_higher_price_version():
    case = setup_case(zone=SignalZone.NORMAL)
    case.repo.state.last_price_at = NOW
    case.repo.state.last_price_version = 9
    result = await case.service.evaluate(
        signal_input(
            case,
            price_at=datetime(2026, 7, 17, 9, 29, tzinfo=UTC),
            quote_scheduled_at=datetime(2026, 7, 17, 9, 29, tzinfo=UTC),
            price_version=10,
        )
    )
    assert result.evaluation.result is EvaluationResult.SUPERSEDED


@pytest.mark.anyio
async def test_quote_snapshot_mismatch_is_superseded_without_state_change():
    case = setup_case()
    command = signal_input(case, "8.50")
    case.quotes.value = SignalQuoteSnapshot(
        cycle_id=command.quote_cycle_id,
        item_id=command.quote_item_id,
        symbol=command.symbol,
        status=QuoteItemStatus.VALID,
        price=Decimal("8.40"),
        quote_time=command.price_at,
        scheduled_at=command.quote_scheduled_at,
        eligible_for_evaluation=True,
        expected_subscription_version=command.subscription_version,
    )

    result = await case.service.evaluate(command)

    assert result.evaluation.result is EvaluationResult.SUPERSEDED
    assert result.evaluation.skip_code == "SIGNAL_INPUT_SUPERSEDED"
    assert case.repo.state.zone == SignalZone.UNKNOWN.value


def _mutation_service(*, zone=SignalZone.HIGH):
    case = setup_case(zone=zone)
    audit, events, jobs = Audit(), Events(), Jobs()
    service = SignalService(
        case.repo,
        subscriptions=Port(case.subscription),
        targets=Port(case.target),
        quotes=case.quotes,
        positions=Port(case.position),
        notifications=case.notifications,
        audit=audit,
        events=events,
        jobs=jobs,
    )
    return case, service, audit, events, jobs


def _command(command_type, case, **changes):
    values = {
        "subscription_id": case.subscription.subscription_id,
        "reason": "manual recovery",
        "expected_version": 1,
        "idempotency_key": "signal-write-1",
        "request_id": "request-1",
        "actor_user_id": "user-1",
        "session_id": "session-1",
        "trusted_ip": "127.0.0.1",
    }
    values.update(changes)
    return command_type(**values)


@pytest.mark.anyio
async def test_reset_is_atomic_versioned_audited_and_schedules_reevaluation():
    case, service, audit, events, jobs = _mutation_service()

    result = await service.reset(_command(SignalStateResetCommand, case))

    assert result.code == "SIGNAL_STATE_RESET"
    assert result.state.zone is SignalZone.UNKNOWN
    assert result.state.version == 2
    assert result.replayed is False
    assert case.repo.state.zone == SignalZone.UNKNOWN.value
    assert len(audit.records) == len(events.items) == len(jobs.items) == 1
    assert events.items[0].event_type == "signal.state_reset"
    assert jobs.items[0].job_type == "SIGNAL_REEVALUATE"
    assert jobs.items[0].queue == "signals"
    assert jobs.items[0].config_snapshot["reason"] == "STATE_RESET"


@pytest.mark.anyio
async def test_reset_replay_does_not_repeat_state_audit_event_or_job():
    case, service, audit, events, jobs = _mutation_service()
    command = _command(SignalStateResetCommand, case)

    first = await service.reset(command)
    second = await service.reset(command)

    assert second.replayed is True
    assert second.reevaluation_job_id == first.reevaluation_job_id
    assert case.repo.state.version == 2
    assert len(audit.records) == len(events.items) == len(jobs.items) == 1


@pytest.mark.anyio
async def test_reset_rejects_stale_version_and_idempotency_content_change():
    case, service, *_ = _mutation_service()
    with pytest.raises(AppError) as stale:
        await service.reset(
            _command(SignalStateResetCommand, case, expected_version=2)
        )
    assert stale.value.code == "SIGNAL_STATE_VERSION_CONFLICT"

    command = _command(SignalStateResetCommand, case)
    await service.reset(command)
    with pytest.raises(AppError) as conflict:
        await service.reset(command.model_copy(update={"reason": "another reason"}))
    assert conflict.value.code == "SIGNAL_IDEMPOTENCY_CONFLICT"


@pytest.mark.anyio
async def test_reevaluate_only_schedules_one_versioned_job():
    case, service, audit, events, jobs = _mutation_service(zone=SignalZone.NORMAL)
    command = _command(SignalReevaluationCommand, case)

    first = await service.reevaluate(command)
    second = await service.reevaluate(command)

    assert first.accepted is True
    assert second.replayed is True
    assert first.reevaluation_job_id == second.reevaluation_job_id
    assert case.repo.state.zone == SignalZone.NORMAL.value
    assert case.repo.state.version == 1
    assert len(audit.records) == len(events.items) == len(jobs.items) == 1
    assert events.items[0].event_type == "signal.evaluation_requested"
    assert jobs.items[0].config_snapshot["reason"] == "MANUAL_CHECK"


@pytest.mark.anyio
async def test_evaluation_writes_completed_transition_and_notification_facts():
    case = setup_case(holding=True)
    events = Events()
    service = SignalService(
        case.repo,
        subscriptions=Port(case.subscription),
        targets=Port(case.target),
        quotes=case.quotes,
        positions=Port(case.position),
        notifications=case.notifications,
        events=events,
    )

    result = await service.evaluate(signal_input(case, price="8.50"))

    assert result.event is not None
    assert [event.event_type for event in events.items] == [
        "signal.evaluation_completed",
        "signal.transitioned",
        "signal.notification_requested",
    ]


@pytest.mark.anyio
async def test_skipped_evaluation_writes_a_traceable_fact():
    case = setup_case(status=SubscriptionStatus.PAUSED)
    events = Events()
    service = SignalService(
        case.repo,
        subscriptions=Port(case.subscription),
        targets=Port(case.target),
        quotes=case.quotes,
        positions=Port(case.position),
        notifications=case.notifications,
        events=events,
    )

    result = await service.evaluate(signal_input(case))

    assert result.result is EvaluationResult.SKIPPED
    assert [event.event_type for event in events.items] == [
        "signal.evaluation_skipped"
    ]
