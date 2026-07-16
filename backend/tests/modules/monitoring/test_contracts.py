import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from long_invest.modules.monitoring.contracts import (
    FrozenSubscription,
    MonitorSubscriptionRevisionView,
    MonitorSubscriptionView,
    OccurrenceStatus,
    ScheduleOccurrenceView,
    StrategyReadinessPort,
    SubscriptionStatus,
    TargetReadinessPort,
)


def test_monitoring_status_values_are_stable() -> None:
    assert {item.value for item in SubscriptionStatus} == {
        "CONFIGURING",
        "ENABLED",
        "PAUSED",
        "ARCHIVED",
    }
    assert {item.value for item in OccurrenceStatus} == {
        "PENDING",
        "CLAIMED",
        "DISPATCHED",
        "MISSED",
        "FAILED",
    }


def test_subscription_and_occurrence_views_are_frozen() -> None:
    subscription = FrozenSubscription(
        subscription_id=uuid4(),
        security_id=uuid4(),
        symbol="600000.SH",
        version=2,
        revision_id=uuid4(),
    )
    view = MonitorSubscriptionView(
        id=subscription.subscription_id,
        security_id=subscription.security_id,
        symbol=subscription.symbol,
        status=SubscriptionStatus.ENABLED,
        version=2,
        current_revision_id=subscription.revision_id,
        archived_at=None,
    )
    occurrence = ScheduleOccurrenceView(
        id=uuid4(),
        occurrence_type="REALTIME_QUOTE",
        schedule_id=uuid4(),
        scheduled_at=datetime(2026, 7, 16, 2, tzinfo=UTC),
        status=OccurrenceStatus.PENDING,
        subscriptions=(subscription,),
    )
    assert occurrence.subscriptions == (subscription,)
    with pytest.raises(ValidationError):
        view.status = SubscriptionStatus.PAUSED


def test_readiness_ports_publish_only_boundary_methods() -> None:
    assert "current_readiness" in TargetReadinessPort.__dict__
    assert "published_version" in StrategyReadinessPort.__dict__
    assert "target_ready" not in TargetReadinessPort.__dict__
    assert "strategy_ready" not in StrategyReadinessPort.__dict__


def test_subscription_revision_rejects_invalid_hysteresis() -> None:
    values = {
        "id": uuid4(),
        "subscription_id": uuid4(),
        "revision_no": 1,
        "schedule_id": None,
        "schedule_revision_id": None,
        "target_mode": "MANUAL",
        "target_version_id": None,
        "strategy_version_id": None,
        "parameter_snapshot": {},
        "hysteresis_ratio": "0.020000",
        "hysteresis_min": "0.020000",
        "notification_mode": "DEFAULT",
    }
    for field in ("hysteresis_ratio", "hysteresis_min"):
        with pytest.raises(ValidationError):
            MonitorSubscriptionRevisionView(**(values | {field: "-0.000001"}))


def test_parameter_snapshot_is_deeply_immutable_and_copied() -> None:
    nested = {"windows": [5, 10], "flags": {"confirmed": True}}
    view = MonitorSubscriptionRevisionView(
        id=uuid4(),
        subscription_id=uuid4(),
        revision_no=1,
        schedule_id=None,
        schedule_revision_id=None,
        target_mode="MANUAL",
        target_version_id=None,
        strategy_version_id=None,
        parameter_snapshot=nested,
        hysteresis_ratio="0.020000",
        hysteresis_min="0.020000",
        notification_mode="DEFAULT",
    )
    nested["windows"].append(20)
    nested["flags"]["confirmed"] = False
    assert view.parameter_snapshot["windows"] == (5, 10)
    assert view.parameter_snapshot["flags"]["confirmed"] is True
    with pytest.raises(TypeError):
        view.parameter_snapshot["new"] = True
    with pytest.raises(TypeError):
        view.parameter_snapshot["flags"]["confirmed"] = False

    expected = {
        "windows": [5, 10],
        "flags": {"confirmed": True},
    }
    assert view.model_dump(mode="json")["parameter_snapshot"] == expected
    assert json.loads(view.model_dump_json())["parameter_snapshot"] == expected
