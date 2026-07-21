from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Any


def freeze_json_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("JSON snapshot must be an object")
    frozen = _freeze_json_value(value, set())
    if not isinstance(frozen, Mapping):  # pragma: no cover - guarded above
        raise ValueError("JSON snapshot must be an object")
    return frozen


def _freeze_json_value(value: Any, active: set[int]) -> Any:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON snapshot numbers must be finite")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("JSON snapshot numbers must be finite")
        return str(value)
    if type(value) is date:
        return value
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in active:
            raise ValueError("JSON snapshot contains a cycle")
        active.add(marker)
        try:
            if any(type(key) is not str for key in value):
                raise ValueError("JSON snapshot object keys must be strings")
            items: dict[str, Any] = {}
            for key in sorted(value):
                items[key] = _freeze_json_value(value[key], active)
            return MappingProxyType(items)
        finally:
            active.remove(marker)
    if isinstance(value, (list, tuple)):
        marker = id(value)
        if marker in active:
            raise ValueError("JSON snapshot contains a cycle")
        active.add(marker)
        try:
            return tuple(_freeze_json_value(item, active) for item in value)
        finally:
            active.remove(marker)
    raise ValueError("JSON snapshot contains an unsupported value")


def thaw_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json_value(item) for item in value]
    return value
