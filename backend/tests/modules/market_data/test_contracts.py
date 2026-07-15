import math
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from long_invest.modules.market_data.contracts import (
    OpenQualityIssue,
    QualityIssueStatus,
    QualitySeverity,
    ResolveQualityIssue,
)


def _open_command(**overrides: object) -> OpenQualityIssue:
    values = {
        "issue_type": "QUOTE_CONFLICT",
        "subject_type": "quote_cycle_item",
        "subject_id": "item-1",
        "symbol": "600000.SH",
        "severity": QualitySeverity.WARNING,
        "evidence": {"sources": ["EASTMONEY", "SINA"]},
        "dedupe_key": "quote:item-1:conflict",
    }
    values.update(overrides)
    return OpenQualityIssue(**values)  # type: ignore[arg-type]


def _resolve_command(**overrides: object) -> ResolveQualityIssue:
    values = {
        "issue_id": uuid4(),
        "action": "RESOLVE",
        "actor_user_id": "user-1",
        "reason": "evidence checked",
    }
    values.update(overrides)
    return ResolveQualityIssue(**values)  # type: ignore[arg-type]


def test_quality_issue_status_values_are_stable() -> None:
    assert [status.value for status in QualityIssueStatus] == [
        "OPEN",
        "REVIEW_REQUIRED",
        "RESOLVED",
        "INVALIDATED",
    ]


def test_quality_severity_values_are_stable() -> None:
    assert [severity.value for severity in QualitySeverity] == [
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    ]


def test_open_quality_issue_accepts_valid_command() -> None:
    command = _open_command()

    assert command.symbol == "600000.SH"
    assert command.requires_review is False


def test_resolve_quality_issue_accepts_all_supported_actions() -> None:
    for action in ("RESOLVE", "INVALIDATE", "SELECT_SOURCE", "REFETCH"):
        selected_source = "SINA" if action == "SELECT_SOURCE" else None
        command = _resolve_command(action=action, selected_source=selected_source)
        assert command.action == action


@pytest.mark.parametrize(
    "field",
    ["issue_type", "subject_type", "subject_id", "dedupe_key"],
)
def test_open_quality_issue_rejects_blank_required_strings(field: str) -> None:
    with pytest.raises(ValueError):
        _open_command(**{field: "  "})


@pytest.mark.parametrize("field", ["action", "actor_user_id", "reason"])
def test_resolve_quality_issue_rejects_blank_required_strings(field: str) -> None:
    with pytest.raises(ValueError):
        _resolve_command(**{field: "  "})


def test_open_quality_issue_rejects_empty_evidence() -> None:
    with pytest.raises(ValueError, match="证据"):
        _open_command(evidence={})


def test_open_quality_issue_rejects_non_json_evidence() -> None:
    with pytest.raises(ValueError, match="证据"):
        _open_command(evidence={"value": object()})


def test_open_quality_issue_rejects_non_finite_number_evidence() -> None:
    with pytest.raises(ValueError, match="证据"):
        _open_command(evidence={"value": math.nan})


def test_open_quality_issue_rejects_invalid_symbol() -> None:
    with pytest.raises(ValueError):
        _open_command(symbol="600000")


def test_open_quality_issue_accepts_missing_symbol() -> None:
    assert _open_command(symbol=None).symbol is None


def test_resolve_quality_issue_rejects_unsupported_action() -> None:
    with pytest.raises(ValueError):
        _resolve_command(action="EDIT_PRICE")


@pytest.mark.parametrize("action", ["SELECT_SOURCE", "RESOLVE"])
def test_resolve_quality_issue_rejects_blank_selected_source(action: str) -> None:
    with pytest.raises(ValueError):
        _resolve_command(action=action, selected_source="  ")


def test_select_source_action_requires_selected_source() -> None:
    with pytest.raises(ValueError):
        _resolve_command(action="SELECT_SOURCE", selected_source=None)


def test_quality_commands_are_frozen() -> None:
    open_command = _open_command()
    resolve_command = _resolve_command()

    with pytest.raises(FrozenInstanceError):
        open_command.subject_id = "item-2"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        resolve_command.reason = "changed"  # type: ignore[misc]
