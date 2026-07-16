from datetime import time

import pytest
from pydantic import ValidationError

from long_invest.modules.monitor_schedules.contracts import ScheduleDefinition


def definition(times) -> ScheduleDefinition:
    return ScheduleDefinition(
        name="盘中监控",
        times=times,
        reason="调整时间",
        idempotency_key="schedule-1",
        expected_version=1,
    )


def test_schedule_times_are_copied_sorted_and_frozen() -> None:
    values = [time(14, 30), time(9, 30), time(13)]
    command = definition(values)
    values.append(time(10))
    assert command.times == (time(9, 30), time(13), time(14, 30))
    with pytest.raises(ValidationError):
        command.times = ()
    assert definition([]).times == ()


@pytest.mark.parametrize(
    "times",
    [
        [time(9, 29)],
        [time(11, 31)],
        [time(12)],
        [time(15, 1)],
        [time(10), time(10)],
        [time(9, 30)] * 21,
        [time(9, 30, 1)],
    ],
)
def test_schedule_rejects_invalid_times(times) -> None:
    with pytest.raises(ValidationError):
        definition(times)


@pytest.mark.parametrize(("name", "reason"), [("   ", "reason"), ("name", "   ")])
def test_schedule_rejects_blank_name_or_reason(name, reason) -> None:
    with pytest.raises(ValidationError):
        ScheduleDefinition(
            name=name,
            times=(),
            reason=reason,
            idempotency_key="key",
        )


@pytest.mark.parametrize("times", [None, "09:30", 123, [None]])
def test_schedule_rejects_null_and_wrong_collection_types(times) -> None:
    with pytest.raises(ValidationError):
        ScheduleDefinition(
            name="name",
            times=times,
            reason="reason",
            idempotency_key="key",
        )


@pytest.mark.parametrize("field", ["name", "reason", "idempotency_key"])
def test_schedule_text_validators_reject_wrong_json_types(field) -> None:
    values = {
        "name": "name",
        "times": (),
        "reason": "reason",
        "idempotency_key": "key",
    }
    values[field] = 123
    with pytest.raises(ValidationError):
        ScheduleDefinition(**values)
