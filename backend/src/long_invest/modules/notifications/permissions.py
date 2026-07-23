from datetime import datetime

from long_invest.modules.notifications.delivery import CircuitSnapshot, CircuitState


def notification_channel_allowed_actions(
    *,
    enabled: bool,
    secret_configured: bool,
    circuit: CircuitSnapshot,
    now: datetime,
) -> tuple[str, ...]:
    actions = ["UPDATE"]
    if not enabled or not secret_configured:
        return tuple(actions)

    actions.append("TEST")
    if (
        circuit.state is CircuitState.OPEN
        and circuit.retry_at is not None
        and now >= circuit.retry_at
    ):
        actions.append("PROBE")
    if circuit.state in {CircuitState.OPEN, CircuitState.HALF_OPEN}:
        actions.append("RESET_CIRCUIT")
    return tuple(actions)


def notification_policy_allowed_actions() -> tuple[str, ...]:
    return ("UPDATE",)


def notification_template_allowed_actions(*, active: bool) -> tuple[str, ...]:
    actions = ["PREVIEW"]
    if not active:
        actions.append("ACTIVATE")
    return tuple(actions)
