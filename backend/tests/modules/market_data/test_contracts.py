import json
import math
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from long_invest.modules.market_data.contracts import (
    AdjustmentTimelineEntry,
    AdjustmentTimelineSnapshot,
    OpenQualityIssue,
    QualityIssuePage,
    QualityIssueStatus,
    QualityIssueView,
    QualityResolutionAction,
    QualitySeverity,
    RequestQualityRefetch,
    ResolveQualityIssue,
)


def test_adjustment_timeline_entry_freezes_effective_and_publication_dates() -> None:
    entry = AdjustmentTimelineEntry(
        event_date=date(2026, 1, 2),
        effective_date=date(2026, 1, 2),
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        source="provider",
        adjustment_factor=Decimal("0.5"),
        data_hash="a" * 64,
    )

    assert entry.effective_date == date(2026, 1, 2)
    with pytest.raises(FrozenInstanceError):
        entry.source = "changed"  # type: ignore[misc]


def test_adjustment_timeline_rejects_naive_publication_and_as_of_times() -> None:
    with pytest.raises(ValueError, match="timezone"):
        AdjustmentTimelineEntry(
            event_date=date(2026, 1, 2),
            effective_date=date(2026, 1, 2),
            published_at=datetime(2026, 1, 1),
            source="provider",
            adjustment_factor=Decimal("0.5"),
            data_hash="a" * 64,
        )

    with pytest.raises(ValueError, match="timezone"):
        AdjustmentTimelineSnapshot(as_of=datetime(2026, 1, 2), entries=())


def test_adjustment_timeline_only_contains_information_known_as_of_snapshot() -> None:
    entry = AdjustmentTimelineEntry(
        event_date=date(2026, 1, 2),
        effective_date=date(2026, 1, 2),
        published_at=datetime(2026, 1, 3, tzinfo=UTC),
        source="provider",
        adjustment_factor=Decimal("0.5"),
        data_hash="a" * 64,
    )

    with pytest.raises(ValueError, match="as_of"):
        AdjustmentTimelineSnapshot(
            as_of=datetime(2026, 1, 2, tzinfo=UTC),
            entries=(entry,),
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


def test_quality_resolution_action_values_are_stable() -> None:
    assert [action.value for action in QualityResolutionAction] == [
        "RESOLVE",
        "INVALIDATE",
        "SELECT_SOURCE",
        "REFETCH",
    ]


def test_open_quality_issue_accepts_valid_command() -> None:
    command = _open_command()

    assert command.symbol == "600000.SH"
    assert command.requires_review is False


def test_open_quality_issue_normalizes_severity_string_to_enum() -> None:
    command = _open_command(severity="ERROR")

    assert command.severity is QualitySeverity.ERROR


def test_open_quality_issue_rejects_invalid_severity() -> None:
    with pytest.raises(ValueError):
        _open_command(severity="URGENT")


def test_open_quality_issue_evidence_is_directly_json_serializable() -> None:
    command = _open_command(evidence={"sources": [{"provider": "SINA", "price": 10.5}]})

    assert json.loads(json.dumps(command.evidence, allow_nan=False)) == {
        "sources": [{"provider": "SINA", "price": 10.5}]
    }


def test_resolve_quality_issue_accepts_all_supported_actions() -> None:
    for action in QualityResolutionAction:
        selected_source = (
            "SINA" if action is QualityResolutionAction.SELECT_SOURCE else None
        )
        command = _resolve_command(action=action, selected_source=selected_source)
        assert command.action is action


def test_resolve_quality_issue_normalizes_action_string_to_enum() -> None:
    command = _resolve_command(action="RESOLVE")

    assert command.action is QualityResolutionAction.RESOLVE


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


def test_open_quality_issue_requires_evidence_object() -> None:
    with pytest.raises(ValueError, match="证据"):
        _open_command(evidence=["not", "an", "object"])


def test_open_quality_issue_rejects_non_json_evidence() -> None:
    with pytest.raises(ValueError, match="证据"):
        _open_command(evidence={"value": object()})


def test_open_quality_issue_rejects_non_finite_number_evidence() -> None:
    with pytest.raises(ValueError, match="证据"):
        _open_command(evidence={"value": math.nan})


@pytest.mark.parametrize("value", [math.inf, -math.inf])
def test_open_quality_issue_rejects_infinite_number_evidence(value: float) -> None:
    with pytest.raises(ValueError, match="证据"):
        _open_command(evidence={"value": value})


def test_open_quality_issue_rejects_non_string_object_keys() -> None:
    with pytest.raises(ValueError, match="证据"):
        _open_command(evidence={1: "integer key"})


@pytest.mark.parametrize("value", [b"bytes", {"set"}, object()])
def test_open_quality_issue_rejects_unsupported_json_leaves(value: object) -> None:
    with pytest.raises(ValueError, match="证据"):
        _open_command(evidence={"value": value})


def test_open_quality_issue_accepts_and_normalizes_json_values() -> None:
    command = _open_command(
        evidence={
            "values": (None, True, 1, 1.5, "text", ["nested"]),
        }
    )

    assert command.evidence["values"] == [
        None,
        True,
        1,
        1.5,
        "text",
        ["nested"],
    ]


def test_open_quality_issue_copies_and_deeply_freezes_evidence() -> None:
    original = {
        "sources": [
            {"provider": "SINA", "quote": {"price": 10.5}},
        ]
    }
    command = _open_command(evidence=original)

    original["added"] = True
    original["sources"].append({"provider": "EASTMONEY"})  # type: ignore[union-attr]
    original["sources"][0]["provider"] = "CHANGED"  # type: ignore[index]
    original["sources"][0]["quote"]["price"] = 0  # type: ignore[index]

    assert "added" not in command.evidence
    assert command.evidence["sources"] == [
        {"provider": "SINA", "quote": {"price": 10.5}},
    ]

    with pytest.raises(TypeError):
        command.evidence["added"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        command.evidence["sources"][0]["provider"] = "CHANGED"  # type: ignore[index]
    with pytest.raises(TypeError):
        command.evidence["sources"].append("new")  # type: ignore[union-attr]


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


@pytest.mark.parametrize("action", ["RESOLVE", "INVALIDATE", "REFETCH"])
def test_non_selection_actions_reject_selected_source(action: str) -> None:
    with pytest.raises(ValueError):
        _resolve_command(action=action, selected_source="SINA")


def test_quality_commands_are_frozen() -> None:
    open_command = _open_command()
    resolve_command = _resolve_command()

    with pytest.raises(FrozenInstanceError):
        open_command.subject_id = "item-2"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        resolve_command.reason = "changed"  # type: ignore[misc]


def test_quality_issue_view_normalizes_enums_and_freezes_evidence() -> None:
    evidence = {"sources": [{"provider": "SINA"}]}
    view = QualityIssueView(
        id=uuid4(),
        issue_type="QUOTE_CONFLICT",
        subject_type="quote_cycle_item",
        subject_id="item-1",
        symbol="600000.SH",
        status="OPEN",
        severity="WARNING",
        evidence=evidence,
        occurrence_count=1,
        first_seen_at=datetime(2026, 7, 15, tzinfo=UTC),
        last_seen_at=datetime(2026, 7, 15, tzinfo=UTC),
        resolved_at=None,
        resolved_by_user_id=None,
        resolution_action=None,
        resolution_reason=None,
        selected_source=None,
    )

    evidence["sources"].append({"provider": "EASTMONEY"})
    assert view.status is QualityIssueStatus.OPEN
    assert view.severity is QualitySeverity.WARNING
    assert json.loads(json.dumps(view.evidence, allow_nan=False)) == {
        "sources": [{"provider": "SINA"}]
    }
    with pytest.raises(TypeError):
        view.evidence["added"] = True  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        view.status = QualityIssueStatus.RESOLVED  # type: ignore[misc]


def test_quality_issue_view_normalizes_optional_resolution_action() -> None:
    view = QualityIssueView(
        id=uuid4(),
        issue_type="QUOTE_CONFLICT",
        subject_type="quote_cycle_item",
        subject_id="item-1",
        symbol=None,
        status="RESOLVED",
        severity="ERROR",
        evidence={"source": "SINA"},
        occurrence_count=2,
        first_seen_at=datetime(2026, 7, 15, tzinfo=UTC),
        last_seen_at=datetime(2026, 7, 15, tzinfo=UTC),
        resolved_at=datetime(2026, 7, 15, tzinfo=UTC),
        resolved_by_user_id="user-1",
        resolution_action="SELECT_SOURCE",
        resolution_reason="checked",
        selected_source="SINA",
    )

    assert view.resolution_action is QualityResolutionAction.SELECT_SOURCE


def test_quality_issue_page_is_frozen_and_uses_tuple_items() -> None:
    page = QualityIssuePage(items=(), total=0, page=1, page_size=50)

    assert page.items == ()
    with pytest.raises(FrozenInstanceError):
        page.total = 1  # type: ignore[misc]


@pytest.mark.parametrize("field", ["actor_user_id", "reason", "idempotency_key"])
def test_request_quality_refetch_rejects_blank_required_text(field: str) -> None:
    values = {
        "issue_id": uuid4(),
        "actor_user_id": "user-1",
        "reason": "retry provider",
        "idempotency_key": "refetch:item-1:1",
    }
    values[field] = "  "

    with pytest.raises(ValueError):
        RequestQualityRefetch(**values)  # type: ignore[arg-type]
