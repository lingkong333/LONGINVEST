from datetime import UTC, date, datetime
from decimal import Decimal
from types import MappingProxyType
from uuid import uuid4

import pytest
from pydantic import ValidationError

from long_invest.modules.targets.contracts import (
    ManualTargetCommand,
    RestoreTargetCommand,
    TargetBindingView,
    TargetRevisionView,
    TargetSnapshot,
    TargetSource,
    TargetStatus,
    TargetValues,
)


def _audit_fields() -> dict[str, object]:
    return {
        "reason": "  rebalance levels  ",
        "expected_version": 1,
        "idempotency_key": "  target-1  ",
        "request_id": "  req-1  ",
        "actor_user_id": "  user-1  ",
        "session_id": "  session-1  ",
        "trusted_ip": "  127.0.0.1  ",
    }


def test_target_enums_are_exact() -> None:
    assert {item.value for item in TargetSource} == {"MANUAL", "RESTORED"}
    assert {item.value for item in TargetStatus} == {
        "READY",
        "STALE",
        "CALCULATING",
        "REVIEW_REQUIRED",
        "ACTIVATING",
        "FAILED",
        "MISSING",
    }


def test_target_values_quantize_half_up_and_are_frozen() -> None:
    values = TargetValues(
        low_strong="1.005",
        low_watch="2.004",
        high_watch="3.005",
        high_strong="4.006",
    )
    assert values.model_dump() == {
        "low_strong": Decimal("1.01"),
        "low_watch": Decimal("2.00"),
        "high_watch": Decimal("3.01"),
        "high_strong": Decimal("4.01"),
    }
    with pytest.raises(ValidationError):
        values.low_strong = Decimal("0.50")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "0", "-1"])
def test_target_values_reject_non_finite_or_non_positive_values(value: str) -> None:
    with pytest.raises(ValidationError):
        TargetValues(
            low_strong=value,
            low_watch="2",
            high_watch="3",
            high_strong="4",
        )


def test_target_values_reject_price_that_rounds_to_zero() -> None:
    with pytest.raises(ValidationError):
        TargetValues(
            low_strong="0.004",
            low_watch="1",
            high_watch="2",
            high_strong="3",
        )


def test_target_values_match_numeric_20_2_capacity() -> None:
    accepted = TargetValues(
        low_strong="1",
        low_watch="2",
        high_watch="3",
        high_strong="999999999999999999.99",
    )
    assert accepted.high_strong == Decimal("999999999999999999.99")
    with pytest.raises(ValidationError):
        TargetValues(
            low_strong="1",
            low_watch="2",
            high_watch="3",
            high_strong="1000000000000000000.00",
        )


@pytest.mark.parametrize(
    "levels",
    [
        ("1", "1", "3", "4"),
        ("1", "3", "2", "4"),
        ("1", "2", "4", "4"),
        ("1.001", "1.004", "2", "3"),
    ],
)
def test_target_values_reject_non_increasing_quantized_levels(
    levels: tuple[str, str, str, str],
) -> None:
    with pytest.raises(ValidationError):
        TargetValues(
            low_strong=levels[0],
            low_watch=levels[1],
            high_watch=levels[2],
            high_strong=levels[3],
        )


def test_manual_command_trims_audit_text_and_forbids_extra_fields() -> None:
    command = ManualTargetCommand(
        subscription_id=uuid4(),
        target_date=date(2026, 7, 17),
        values=TargetValues(
            low_strong="1", low_watch="2", high_watch="3", high_strong="4"
        ),
        large_change_confirmed=True,
        switch_to_manual_confirmed=True,
        **_audit_fields(),
    )
    assert command.reason == "rebalance levels"
    assert command.idempotency_key == "target-1"
    assert command.trusted_ip == "127.0.0.1"
    with pytest.raises(ValidationError):
        ManualTargetCommand.model_validate(command.model_dump() | {"unexpected": True})


def test_restore_command_freezes_source_revision_and_audit_context() -> None:
    source_revision_id = uuid4()
    command = RestoreTargetCommand(
        subscription_id=uuid4(),
        source_revision_id=source_revision_id,
        switch_to_manual_confirmed=True,
        **_audit_fields(),
    )
    assert command.source_revision_id == source_revision_id
    assert command.request_id == "req-1"
    with pytest.raises(ValidationError):
        command.reason = "changed"


@pytest.mark.parametrize(
    "field",
    [
        "reason",
        "idempotency_key",
        "request_id",
        "actor_user_id",
        "session_id",
        "trusted_ip",
    ],
)
def test_target_commands_reject_blank_audit_text(field: str) -> None:
    audit = _audit_fields()
    audit[field] = "   "
    with pytest.raises(ValidationError):
        RestoreTargetCommand(
            subscription_id=uuid4(),
            source_revision_id=uuid4(),
            switch_to_manual_confirmed=True,
            **audit,
        )


def test_target_views_freeze_reproducibility_snapshot_and_serialize_mapping() -> None:
    revision_id = uuid4()
    subscription_id = uuid4()
    created_at = datetime(2026, 7, 17, 10, tzinfo=UTC)
    values = TargetValues(
        low_strong="1", low_watch="2", high_watch="3", high_strong="4"
    )
    revision = TargetRevisionView(
        id=revision_id,
        subscription_id=subscription_id,
        revision_no=1,
        values=values,
        source=TargetSource.MANUAL,
        target_date=date(2026, 7, 17),
        strategy_version_id=None,
        parameter_snapshot={"window": 20, "nested": {"enabled": True}},
        data_version=None,
        source_code_hash=None,
        content_hash="a" * 64,
        reason="manual",
        created_at=created_at,
    )
    assert isinstance(revision.parameter_snapshot, MappingProxyType)
    assert isinstance(revision.parameter_snapshot["nested"], MappingProxyType)
    assert revision.model_dump(mode="json")["parameter_snapshot"] == {
        "window": 20,
        "nested": {"enabled": True},
    }
    snapshot = TargetSnapshot(
        subscription_id=subscription_id,
        revision_id=revision_id,
        revision_no=1,
        binding_version=1,
        values=values,
        source=TargetSource.MANUAL,
        status=TargetStatus.READY,
        target_date=revision.target_date,
        strategy_version_id=None,
        parameter_snapshot=revision.parameter_snapshot,
        data_version=None,
        source_code_hash=None,
        content_hash=revision.content_hash,
        activated_at=created_at,
    )
    assert snapshot.content_hash == "a" * 64


@pytest.mark.parametrize(
    ("source", "source_revision_id", "valid"),
    [
        (TargetSource.MANUAL, None, True),
        (TargetSource.RESTORED, uuid4(), True),
        (TargetSource.MANUAL, uuid4(), False),
        (TargetSource.RESTORED, None, False),
    ],
)
def test_target_revision_source_matches_source_revision(
    source: TargetSource, source_revision_id: object, valid: bool
) -> None:
    values = {
        "id": uuid4(),
        "subscription_id": uuid4(),
        "revision_no": 1,
        "values": TargetValues(
            low_strong="1", low_watch="2", high_watch="3", high_strong="4"
        ),
        "source": source,
        "source_revision_id": source_revision_id,
        "target_date": date(2026, 7, 17),
        "parameter_snapshot": {},
        "content_hash": "a" * 64,
        "reason": "manual or restored",
        "created_at": datetime(2026, 7, 17, 10, tzinfo=UTC),
    }
    if valid:
        revision = TargetRevisionView(**values)
        assert revision.source_revision_id == source_revision_id
    else:
        with pytest.raises(ValidationError):
            TargetRevisionView(**values)


@pytest.mark.parametrize("view", ["revision", "binding", "snapshot"])
def test_target_views_reject_naive_business_times(view: str) -> None:
    common = {
        "subscription_id": uuid4(),
        "values": TargetValues(
            low_strong="1", low_watch="2", high_watch="3", high_strong="4"
        ),
    }
    naive = datetime(2026, 7, 17, 10)
    with pytest.raises(ValidationError):
        if view == "revision":
            TargetRevisionView(
                id=uuid4(),
                revision_no=1,
                source=TargetSource.MANUAL,
                target_date=date(2026, 7, 17),
                parameter_snapshot={},
                content_hash="a" * 64,
                reason="manual",
                created_at=naive,
                **common,
            )
        elif view == "binding":
            TargetBindingView(
                subscription_id=common["subscription_id"],
                current_revision_id=uuid4(),
                status=TargetStatus.READY,
                version=1,
                activated_at=naive,
            )
        else:
            TargetSnapshot(
                revision_id=uuid4(),
                revision_no=1,
                binding_version=1,
                source=TargetSource.MANUAL,
                status=TargetStatus.READY,
                target_date=date(2026, 7, 17),
                parameter_snapshot={},
                content_hash="a" * 64,
                activated_at=naive,
                **common,
            )
