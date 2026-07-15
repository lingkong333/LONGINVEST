from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from long_invest.modules.providers.contracts import validate_symbol


class QualityIssueStatus(StrEnum):
    OPEN = "OPEN"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    RESOLVED = "RESOLVED"
    INVALIDATED = "INVALIDATED"


class QualitySeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


_QUALITY_ACTIONS = frozenset(
    {"RESOLVE", "INVALIDATE", "SELECT_SOURCE", "REFETCH"}
)


def _require_text(value: str, field: str) -> None:
    if not value.strip():
        raise ValueError(f"{field} 不能为空")


@dataclass(frozen=True, slots=True)
class OpenQualityIssue:
    issue_type: str
    subject_type: str
    subject_id: str
    symbol: str | None
    severity: QualitySeverity
    evidence: Mapping[str, object]
    dedupe_key: str
    requires_review: bool = False

    def __post_init__(self) -> None:
        _require_text(self.issue_type, "问题类型")
        _require_text(self.subject_type, "关联对象类型")
        _require_text(self.subject_id, "关联对象编号")
        _require_text(self.dedupe_key, "去重键")
        if self.symbol is not None:
            _require_text(self.symbol, "股票代码")
            validate_symbol(self.symbol)
        if not self.evidence:
            raise ValueError("质量问题证据不能为空")
        try:
            json.dumps(self.evidence, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("质量问题证据必须可安全序列化为 JSON") from exc


@dataclass(frozen=True, slots=True)
class ResolveQualityIssue:
    issue_id: UUID
    action: str
    actor_user_id: str
    reason: str
    selected_source: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.action, "处理动作")
        _require_text(self.actor_user_id, "处理用户")
        _require_text(self.reason, "处理原因")
        if self.action not in _QUALITY_ACTIONS:
            raise ValueError("不支持的质量问题处理动作")
        if self.action == "SELECT_SOURCE" and self.selected_source is None:
            raise ValueError("选择来源不能为空")
        if self.selected_source is not None:
            _require_text(self.selected_source, "选择来源")
