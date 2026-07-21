from decimal import Decimal
from types import MappingProxyType

import pytest
from pydantic import TypeAdapter, ValidationError

from long_invest.platform.json_snapshot import (
    freeze_json_mapping,
    thaw_json_value,
)
from long_invest.platform.validation import Sha256Hex


def test_json_snapshot_is_deeply_frozen_and_deterministically_thawed() -> None:
    frozen = freeze_json_mapping(
        {
            "z": (Decimal("1.20"), {"enabled": True}),
            "a": [None, "text", 3, 1.5],
        }
    )

    assert isinstance(frozen, MappingProxyType)
    assert isinstance(frozen["z"], tuple)
    assert isinstance(frozen["z"][1], MappingProxyType)
    thawed = thaw_json_value(frozen)
    assert list(thawed) == ["a", "z"]
    assert thawed == {
        "a": [None, "text", 3, 1.5],
        "z": ["1.20", {"enabled": True}],
    }
    assert thawed["z"][1]["enabled"] is True


@pytest.mark.parametrize(
    "value",
    [
        {"unsupported": {1, 2}},
        {"unsupported": frozenset({1, 2})},
        {"unsupported": object()},
        {"unsupported": float("nan")},
        {"unsupported": float("inf")},
        {1: "non-string key"},
    ],
)
def test_json_snapshot_rejects_unsupported_or_nondeterministic_values(
    value: object,
) -> None:
    with pytest.raises(ValueError):
        freeze_json_mapping(value)  # type: ignore[arg-type]


def test_json_snapshot_rejects_mapping_and_sequence_cycles() -> None:
    mapping_cycle: dict[str, object] = {}
    mapping_cycle["self"] = mapping_cycle
    sequence_cycle: list[object] = []
    sequence_cycle.append(sequence_cycle)

    with pytest.raises(ValueError, match="cycle"):
        freeze_json_mapping(mapping_cycle)
    with pytest.raises(ValueError, match="cycle"):
        freeze_json_mapping({"items": sequence_cycle})


@pytest.mark.parametrize("value", ["!" * 64, "A" * 64, "a" * 63])
def test_sha256_type_rejects_non_lowercase_hex(value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Sha256Hex).validate_python(value)
