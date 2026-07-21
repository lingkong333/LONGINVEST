from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import NoReturn, Protocol
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


@dataclass(frozen=True, slots=True)
class AdjustmentTimelineEntry:
    event_date: date
    effective_date: date
    published_at: datetime
    source: str
    adjustment_factor: Decimal
    data_hash: str

    def __post_init__(self) -> None:
        _require_text(self.source, "调整来源")
        if not self.adjustment_factor.is_finite() or self.adjustment_factor <= 0:
            raise ValueError("adjustment factor must be finite and positive")
        if len(self.data_hash) != 64:
            raise ValueError("adjustment data hash must be sha256")


class AdjustmentTimelinePort(Protocol):
    async def get_adjustment_timeline(
        self,
        *,
        security_id: UUID,
        start_date: date,
        end_date: date,
        as_of: datetime,
    ) -> tuple[AdjustmentTimelineEntry, ...]: ...


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


class _FrozenDict(dict[str, object]):
    def _immutable(self, *args: object, **kwargs: object) -> NoReturn:
        raise TypeError("质量问题证据不可修改")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


class _FrozenList(list[object]):
    def _immutable(self, *args: object, **kwargs: object) -> NoReturn:
        raise TypeError("质量问题证据不可修改")

    __setitem__ = _immutable
    __delitem__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable


def _freeze_json_value(value: object) -> object:
    if isinstance(value, dict):
        return _FrozenDict(
            (key, _freeze_json_value(item)) for key, item in value.items()
        )
    if isinstance(value, list):
        return _FrozenList(_freeze_json_value(item) for item in value)
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
        try:
            severity = QualitySeverity(self.severity)
        except (TypeError, ValueError) as exc:
            raise ValueError("不支持的数据质量问题严重程度") from exc
        object.__setattr__(self, "severity", severity)
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
class QualityIssueView:
    id: UUID
    issue_type: str
    subject_type: str
    subject_id: str
    symbol: str | None
    status: QualityIssueStatus
    severity: QualitySeverity
    evidence: Mapping[str, object]
    occurrence_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None
    resolved_by_user_id: str | None
    resolution_action: QualityResolutionAction | None
    resolution_reason: str | None
    selected_source: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", QualityIssueStatus(self.status))
        object.__setattr__(self, "severity", QualitySeverity(self.severity))
        if self.resolution_action is not None:
            object.__setattr__(
                self,
                "resolution_action",
                QualityResolutionAction(self.resolution_action),
            )
        if not isinstance(self.evidence, Mapping) or not self.evidence:
            raise ValueError("数据质量问题证据必须是非空 JSON 对象")
        try:
            json_value = _copy_json_value(self.evidence, set())
            json.dumps(json_value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("数据质量问题证据必须可安全序列化为 JSON") from exc
        object.__setattr__(self, "evidence", _freeze_json_value(json_value))


@dataclass(frozen=True, slots=True)
class QualityIssuePage:
    items: tuple[QualityIssueView, ...]
    total: int
    page: int
    page_size: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        if self.total < 0:
            raise ValueError("total must not be negative")
        if self.page < 1 or self.page_size < 1:
            raise ValueError("page and page_size must be positive")


@dataclass(frozen=True, slots=True)
class RequestQualityRefetch:
    issue_id: UUID
    actor_user_id: str
    reason: str
    idempotency_key: str

    def __post_init__(self) -> None:
        _require_text(self.actor_user_id, "请求用户")
        _require_text(self.reason, "重取原因")
        _require_text(self.idempotency_key, "幂等键")


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
