from datetime import UTC, datetime, timedelta

from long_invest.modules.notifications.delivery import (
    CircuitSnapshot,
    CircuitState,
)
from long_invest.modules.notifications.permissions import (
    notification_channel_allowed_actions,
    notification_policy_allowed_actions,
    notification_template_allowed_actions,
)

NOW = datetime(2026, 7, 23, tzinfo=UTC)


def test_channel_update_is_available_when_configuration_is_incomplete() -> None:
    actions = notification_channel_allowed_actions(
        enabled=False,
        secret_configured=False,
        circuit=CircuitSnapshot.closed(),
        now=NOW,
    )

    assert actions == ("UPDATE",)


def test_ready_closed_channel_can_be_tested_without_circuit_controls() -> None:
    actions = notification_channel_allowed_actions(
        enabled=True,
        secret_configured=True,
        circuit=CircuitSnapshot.closed(),
        now=NOW,
    )

    assert actions == ("UPDATE", "TEST")


def test_open_channel_only_allows_probe_after_cooldown() -> None:
    cooling_down = CircuitSnapshot(
        CircuitState.OPEN,
        consecutive_failures=3,
        cooldown_level=0,
        retry_at=NOW + timedelta(seconds=60),
    )
    ready = CircuitSnapshot(
        CircuitState.OPEN,
        consecutive_failures=3,
        cooldown_level=0,
        retry_at=NOW,
    )

    assert notification_channel_allowed_actions(
        enabled=True,
        secret_configured=True,
        circuit=cooling_down,
        now=NOW,
    ) == ("UPDATE", "TEST", "RESET_CIRCUIT")
    assert notification_channel_allowed_actions(
        enabled=True,
        secret_configured=True,
        circuit=ready,
        now=NOW,
    ) == ("UPDATE", "TEST", "PROBE", "RESET_CIRCUIT")


def test_half_open_channel_cannot_start_a_second_probe() -> None:
    actions = notification_channel_allowed_actions(
        enabled=True,
        secret_configured=True,
        circuit=CircuitSnapshot(
            CircuitState.HALF_OPEN,
            consecutive_failures=3,
            cooldown_level=0,
            retry_at=None,
        ),
        now=NOW,
    )

    assert actions == ("UPDATE", "TEST", "RESET_CIRCUIT")


def test_disabled_circuit_cannot_be_reset_from_channel_controls() -> None:
    actions = notification_channel_allowed_actions(
        enabled=True,
        secret_configured=True,
        circuit=CircuitSnapshot(
            CircuitState.DISABLED,
            consecutive_failures=0,
            cooldown_level=0,
            retry_at=None,
        ),
        now=NOW,
    )

    assert actions == ("UPDATE", "TEST")


def test_policy_and_template_actions_are_explicit() -> None:
    assert notification_policy_allowed_actions() == ("UPDATE",)
    assert notification_template_allowed_actions(active=True) == ("PREVIEW",)
    assert notification_template_allowed_actions(active=False) == (
        "PREVIEW",
        "ACTIVATE",
    )
