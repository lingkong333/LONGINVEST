from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.modules.targets.contracts import TargetValues
from long_invest.modules.targets.strategy_service import (
    ApplyStrategyTargetCommand,
    CalculateTargetCommand,
    ReviewCommand,
    StrategyTargetService,
)
from long_invest.platform.errors import AppError


class Repository:
    def __init__(self):
        self.binding = None
        self.runs = {}
        self.revisions = {}
        self.reviews = {}

    async def lock_binding(self, _subscription_id):
        return self.binding

    async def create_binding(self, subscription_id):
        self.binding = SimpleNamespace(
            subscription_id=subscription_id,
            current_revision_id=None,
            status="MISSING",
            version=1,
            activated_at=None,
            stale_reason=None,
        )
        return self.binding

    async def get_calculation_by_idempotency(self, subscription_id, key):
        return next(
            (
                row
                for row in self.runs.values()
                if row.subscription_id == subscription_id and row.idempotency_key == key
            ),
            None,
        )

    async def persist_calculation(self, run):
        self.runs[run.id] = run

    async def get_calculation(self, run_id, *, for_update=False):
        return self.runs.get(run_id)

    async def get_revision(self, revision_id):
        return self.revisions.get(revision_id)

    async def find_revision_by_idempotency(self, subscription_id, key):
        return next(
            (
                row
                for row in self.revisions.values()
                if row.subscription_id == subscription_id and row.idempotency_key == key
            ),
            None,
        )

    async def persist_revision(self, revision):
        self.revisions[revision.id] = revision

    async def next_revision_no(self, _subscription_id):
        return len(self.revisions) + 1

    async def persist_review(self, review):
        self.reviews[review.id] = review

    async def get_review(self, review_id, *, for_update=False):
        return self.reviews.get(review_id)

    async def get_review_by_candidate(self, revision_id):
        return next(
            (
                row
                for row in self.reviews.values()
                if row.candidate_revision_id == revision_id
            ),
            None,
        )

    async def list_pending_reviews_for_subscription(self, subscription_id):
        return tuple(
            review
            for review in self.reviews.values()
            if review.status == "PENDING"
            and self.revisions[review.candidate_revision_id].subscription_id
            == subscription_id
        )

    async def flush(self):
        return None


class Collector:
    def __init__(self):
        self.items = []

    async def append(self, item):
        self.items.append(item)

    async def find_by_idempotency(self, key):
        return next((item for item in self.items if item.idempotency_key == key), None)


class Subscriptions:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    async def lock(self, _subscription_id):
        return self.snapshot

    async def switch_to_strategy(self, **kwargs):
        self.snapshot.strategy_version_id = kwargs["strategy_version_id"]
        self.snapshot.parameter_snapshot = dict(kwargs["parameters"])
        self.snapshot.version += 1
        self.snapshot.revision_id = uuid4()
        return SimpleNamespace(code="MONITOR_SUBSCRIPTION_UPDATED")


@pytest.fixture
def setup():
    subscription_id = uuid4()
    strategy_version_id = uuid4()
    snapshot = SimpleNamespace(
        subscription_id=subscription_id,
        security_id=uuid4(),
        symbol="600000.SH",
        status="ACTIVE",
        version=3,
        revision_id=uuid4(),
        target_mode="STRATEGY",
        strategy_version_id=strategy_version_id,
        parameter_snapshot={"window": 20},
    )
    repository = Repository()
    audit = Collector()
    events = Collector()
    service = StrategyTargetService(
        repository,
        subscriptions=Subscriptions(snapshot),
        audit=audit,
        events=events,
        now=lambda: datetime(2026, 7, 21, tzinfo=UTC),
    )
    command = CalculateTargetCommand(
        subscription_id=subscription_id,
        target_date=date(2026, 7, 21),
        training_start_date=date(2020, 1, 1),
        training_end_date=date(2025, 12, 31),
        reason="重新计算",
        expected_version=1,
        idempotency_key="calc-1",
        request_id="req-1",
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
    )
    return service, repository, audit, events, snapshot, command


def values(base: str) -> TargetValues:
    number = Decimal(base)
    return TargetValues(
        low_strong=number,
        low_watch=number + 1,
        high_watch=number + 2,
        high_strong=number + 3,
    )


@pytest.mark.anyio
async def test_first_calculation_activates_and_requests_signal_reevaluation(setup):
    service, repository, audit, events, _, command = setup
    reserved = await service.reserve(command)
    await service.mark_running(reserved.run_id, data_version=7)
    result = await service.complete(
        reserved.run_id,
        values=values("10"),
        target_date=command.target_date,
        source_code_hash="a" * 64,
        current_data_version=7,
    )

    assert result.code == "TARGET_CALCULATION_SUCCEEDED"
    assert repository.binding.current_revision_id == result.revision_id
    assert repository.binding.status == "READY"
    assert [event.event_type for event in events.items][-2:] == [
        "target.activated",
        "signal.reevaluation_requested",
    ]
    assert len(audit.items) == 1

    replay = await service.result(reserved.run_id, replayed=True)
    assert replay.revision_id == result.revision_id
    assert replay.code == result.code
    assert replay.replayed is True


@pytest.mark.anyio
async def test_large_change_keeps_baseline_and_creates_review(setup):
    service, repository, _, _, _, command = setup
    first = await service.reserve(command)
    await service.mark_running(first.run_id, data_version=7)
    baseline = await service.complete(
        first.run_id,
        values=values("10"),
        target_date=command.target_date,
        source_code_hash="a" * 64,
        current_data_version=7,
    )
    command2 = replace(command, expected_version=3, idempotency_key="calc-2")
    second = await service.reserve(command2)
    await service.mark_running(second.run_id, data_version=8)
    result = await service.complete(
        second.run_id,
        values=values("20"),
        target_date=command.target_date,
        source_code_hash="a" * 64,
        current_data_version=8,
    )

    assert result.code == "TARGET_REVIEW_REQUIRED"
    assert repository.binding.current_revision_id == baseline.revision_id
    assert repository.binding.status == "REVIEW_REQUIRED"

    stale = await service.decide(
        ReviewCommand(
            review_id=result.review_id,
            comment="数据已变化",
            expected_version=4,
            idempotency_key="review-stale-data",
            request_id="req-stale",
            actor_user_id="reviewer",
            session_id="session",
            trusted_ip="127.0.0.1",
            current_data_version=9,
        ),
        approve=True,
    )
    assert stale.code == "TARGET_REVIEW_STALE"
    assert repository.reviews[result.review_id].status == "SUPERSEDED"

    recovered_command = replace(command, expected_version=4, idempotency_key="calc-3")
    recovered = await service.reserve(recovered_command)
    await service.mark_running(recovered.run_id, data_version=9)
    unchanged = await service.complete(
        recovered.run_id,
        values=values("10"),
        target_date=command.target_date,
        source_code_hash="a" * 64,
        current_data_version=9,
    )
    assert unchanged.code == "TARGET_CALCULATION_UNCHANGED"
    assert repository.binding.status == "READY"


@pytest.mark.anyio
async def test_late_result_is_failed_instead_of_overwriting(setup):
    service, repository, _, _, snapshot, command = setup
    reserved = await service.reserve(command)
    await service.mark_running(reserved.run_id, data_version=7)
    snapshot.version += 1
    result = await service.complete(
        reserved.run_id,
        values=values("10"),
        target_date=command.target_date,
        source_code_hash="a" * 64,
        current_data_version=7,
    )

    assert result.code == "TARGET_CALCULATION_FAILED"
    assert repository.runs[reserved.run_id].failure_code == "TARGET_CALCULATION_FAILED"
    assert repository.runs[reserved.run_id].error_summary.startswith(
        "TARGET_CALCULATION_STALE"
    )
    assert repository.binding.current_revision_id is None


@pytest.mark.anyio
async def test_newer_calculation_invalidates_older_in_flight_result(setup):
    service, repository, _, _, _, command = setup
    older = await service.reserve(command)
    newer_command = replace(command, expected_version=2, idempotency_key="calc-2")
    newer = await service.reserve(newer_command)
    await service.mark_running(older.run_id, data_version=7)
    await service.mark_running(newer.run_id, data_version=7)

    old_result = await service.complete(
        older.run_id,
        values=values("10"),
        target_date=command.target_date,
        source_code_hash="a" * 64,
        current_data_version=7,
    )
    new_result = await service.complete(
        newer.run_id,
        values=values("10"),
        target_date=command.target_date,
        source_code_hash="a" * 64,
        current_data_version=7,
    )

    assert old_result.code == "TARGET_CALCULATION_FAILED"
    assert new_result.code == "TARGET_CALCULATION_SUCCEEDED"
    assert repository.binding.current_revision_id == new_result.revision_id


@pytest.mark.anyio
async def test_late_failure_cannot_mark_newer_target_stale(setup):
    service, repository, _, _, _, command = setup
    older = await service.reserve(command)
    newer = await service.reserve(
        replace(command, expected_version=2, idempotency_key="calc-2")
    )
    await service.mark_running(older.run_id, data_version=7)
    await service.mark_running(newer.run_id, data_version=7)
    activated = await service.complete(
        newer.run_id,
        values=values("10"),
        target_date=command.target_date,
        source_code_hash="a" * 64,
        current_data_version=7,
    )

    await service.fail(older.run_id, code="TIMEOUT", summary="late timeout")

    assert repository.binding.current_revision_id == activated.revision_id
    assert repository.binding.status == "READY"


@pytest.mark.anyio
async def test_same_idempotency_key_replays_and_changed_request_conflicts(setup):
    service, _, _, _, _, command = setup
    first = await service.reserve(command)
    replay = await service.reserve(command)
    assert replay.run_id == first.run_id
    assert replay.replayed is True

    changed = replace(command, reason="另一个请求")
    with pytest.raises(AppError) as error:
        await service.reserve(changed)
    assert error.value.code == "TARGET_IDEMPOTENCY_CONFLICT"


@pytest.mark.anyio
async def test_apply_strategy_switches_subscription_and_freezes_new_revision(setup):
    service, repository, _, _, snapshot, command = setup
    new_strategy = uuid4()
    applied = await service.apply_and_reserve(
        ApplyStrategyTargetCommand(
            calculation=command,
            strategy_version_id=new_strategy,
            parameter_snapshot={"window": 60},
            expected_subscription_version=3,
        )
    )

    run = repository.runs[applied.run_id]
    assert run.strategy_version_id == new_strategy
    assert run.subscription_revision_id == snapshot.revision_id
    assert run.subscription_version == 4
    assert run.parameter_snapshot == {"window": 60}


@pytest.mark.anyio
async def test_approve_review_activates_candidate(setup):
    service, repository, _, events, _, command = setup
    first = await service.reserve(command)
    await service.mark_running(first.run_id, data_version=7)
    await service.complete(
        first.run_id,
        values=values("10"),
        target_date=command.target_date,
        source_code_hash="a" * 64,
        current_data_version=7,
    )
    command2 = replace(command, expected_version=3, idempotency_key="calc-2")
    second = await service.reserve(command2)
    await service.mark_running(second.run_id, data_version=8)
    pending = await service.complete(
        second.run_id,
        values=values("20"),
        target_date=command.target_date,
        source_code_hash="a" * 64,
        current_data_version=8,
    )
    result = await service.decide(
        ReviewCommand(
            review_id=pending.review_id,
            comment="同意调整",
            expected_version=4,
            idempotency_key="review-1",
            request_id="req-review",
            actor_user_id="reviewer",
            session_id="session",
            trusted_ip="127.0.0.1",
            current_data_version=8,
        ),
        approve=True,
    )

    assert result.code == "TARGET_REVIEW_APPROVED"
    assert repository.binding.current_revision_id == pending.revision_id
    assert repository.reviews[pending.review_id].status == "APPROVED"
    assert events.items[-1].event_type == "signal.reevaluation_requested"

    replay = await service.decide(
        ReviewCommand(
            review_id=pending.review_id,
            comment="同意调整",
            expected_version=4,
            idempotency_key="review-1",
            request_id="req-review-replay",
            actor_user_id="reviewer",
            session_id="session",
            trusted_ip="127.0.0.1",
            current_data_version=8,
        ),
        approve=True,
    )
    assert replay.replayed is True
    assert replay.revision_id == pending.revision_id

    with pytest.raises(AppError) as changed_comment:
        await service.decide(
            replace(
                ReviewCommand(
                    review_id=pending.review_id,
                    comment="同意调整",
                    expected_version=4,
                    idempotency_key="review-1",
                    request_id="req-review",
                    actor_user_id="reviewer",
                    session_id="session",
                    trusted_ip="127.0.0.1",
                    current_data_version=8,
                ),
                comment="同意，但备注不同",
            ),
            approve=True,
        )
    assert changed_comment.value.code == "TARGET_IDEMPOTENCY_CONFLICT"

    with pytest.raises(AppError) as conflict:
        await service.decide(
            ReviewCommand(
                review_id=pending.review_id,
                comment="改为拒绝",
                expected_version=4,
                idempotency_key="review-1",
                request_id="req-review-conflict",
                actor_user_id="reviewer",
                session_id="session",
                trusted_ip="127.0.0.1",
                current_data_version=8,
            ),
            approve=False,
        )
    assert conflict.value.code == "TARGET_IDEMPOTENCY_CONFLICT"
