from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
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


class QualityResolutionAction(StrEnum):
    RESOLVE = "RESOLVE"
    INVALIDATE = "INVALIDATE"
    SELECT_SOURCE = "SELECT_SOURCE"
    REFETCH = "REFETCH"


def _require_text(value: str, field: str) -> None:
    if not value.strip():
        raise ValueError(f"{field} 不能为空")


def _copy_json_value(value: object, active: set[int]) -> object:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("质量问题证据不能包含非有限数字")
        return value
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in active:
            raise ValueError("质量问题证据不能循环引用")
        active.add(marker)
        try:
            copied: dict[str, object] = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("质量问题证据的对象键必须是字符串")
                copied[key] = _copy_json_value(item, active)
            return copied
        finally:
            active.remove(marker)
    if isinstance(value, (list, tuple)):
        marker = id(value)
        if marker in active:
            raise ValueError("质量问题证据不能循环引用")
        active.add(marker)
        try:
            return [_copy_json_value(item, active) for item in value]
        finally:
            active.remove(marker)
    raise ValueError("质量问题证据包含不支持的 JSON 值")


def _freeze_json_value(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


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
        if not isinstance(self.evidence, Mapping):
            raise ValueError("质量问题证据必须是 JSON 对象")
        if not self.evidence:
            raise ValueError("质量问题证据不能为空")
        try:
            json_value = _copy_json_value(self.evidence, set())
            json.dumps(json_value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("质量问题证据必须可安全序列化为 JSON") from exc
        object.__setattr__(self, "evidence", _freeze_json_value(json_value))


@dataclass(frozen=True, slots=True)
class ResolveQualityIssue:
    issue_id: UUID
    action: QualityResolutionAction
    actor_user_id: str
    reason: str
    selected_source: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.actor_user_id, "处理用户")
        _require_text(self.reason, "处理原因")
        try:
            action = QualityResolutionAction(self.action)
        except (TypeError, ValueError) as exc:
            raise ValueError("不支持的质量问题处理动作") from exc
        object.__setattr__(self, "action", action)
        if action is QualityResolutionAction.SELECT_SOURCE:
            if self.selected_source is None:
                raise ValueError("选择来源不能为空")
            _require_text(self.selected_source, "选择来源")
        elif self.selected_source is not None:
            raise ValueError("仅选择来源动作可以携带选择来源")
