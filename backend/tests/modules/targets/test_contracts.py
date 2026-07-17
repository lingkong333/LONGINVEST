from dataclasses import FrozenInstanceError
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from long_invest.modules.targets.contracts import (
    ManualTargetCommand,
    RestoreTargetCommand,
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
    with pytest.raises((ValidationError, FrozenInstanceError)):
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
