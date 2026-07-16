from uuid import uuid4

import pytest
from pydantic import ValidationError

from long_invest.modules.positions.contracts import (
    PositionHistoryView,
    PositionStatus,
    PositionView,
    SetPosition,
)


def test_set_position_is_strict_trimmed_and_frozen() -> None:
    command = SetPosition(
        security_id=uuid4(),
        symbol="600000.SH",
        target=PositionStatus.HOLDING,
        note="  长期持有  ",
        source="WEB",
        request_id="req-1",
        idempotency_key="position-1",
        actor_user_id="user-1",
        expected_version=1,
    )
    assert command.note == "长期持有"
    with pytest.raises(ValidationError):
        command.target = PositionStatus.NOT_HOLDING
    with pytest.raises(ValidationError):
        command.model_copy(update={"note": "x" * 501}).model_validate(
            command.model_dump() | {"note": "x" * 501}
        )

    trimmed = command.model_validate(command.model_dump() | {"note": f" {'x' * 500} "})
    assert trimmed.note == "x" * 500


@pytest.mark.parametrize(
    "field", ["source", "request_id", "idempotency_key", "actor_user_id"]
)
def test_set_position_rejects_blank_required_text(field) -> None:
    values = {
        "security_id": uuid4(),
        "symbol": "600000.SH",
        "target": PositionStatus.HOLDING,
        "source": "WEB",
        "request_id": "req-1",
        "idempotency_key": "position-1",
        "actor_user_id": "user-1",
    }
    values[field] = "   "
    with pytest.raises(ValidationError):
        SetPosition(**values)


@pytest.mark.parametrize(
    "field", ["note", "source", "request_id", "idempotency_key", "actor_user_id"]
)
def test_set_position_text_validators_reject_wrong_json_types(field) -> None:
    values = {
        "security_id": uuid4(),
        "symbol": "600000.SH",
        "target": PositionStatus.HOLDING,
        "note": "note",
        "source": "WEB",
        "request_id": "req-1",
        "idempotency_key": "position-1",
        "actor_user_id": "user-1",
    }
    values[field] = 123
    with pytest.raises(ValidationError):
        SetPosition(**values)


def test_position_status_and_views_are_frozen() -> None:
    assert {item.value for item in PositionStatus} == {"HOLDING", "NOT_HOLDING"}
    view = PositionView(
        security_id=uuid4(),
        symbol="600000.SH",
        status=PositionStatus.NOT_HOLDING,
        version=0,
    )
    history = PositionHistoryView(
        id=uuid4(),
        security_id=view.security_id,
        before_status=PositionStatus.NOT_HOLDING,
        after_status=PositionStatus.HOLDING,
        version=1,
        note=None,
    )
    assert view.version == 0
    assert history.after_status is PositionStatus.HOLDING
