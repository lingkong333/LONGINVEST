import re
from collections.abc import Mapping

_DAILY_BAR_PARTITION = re.compile(r"daily_bar_unadjusted_\d{4}")


def include_database_name(
    name: str | None,
    type_: str,
    parent_names: Mapping[str, str | None],
) -> bool:
    """Exclude migration-managed partition children from schema diffing."""
    del parent_names
    return not (
        type_ == "table"
        and name is not None
        and _DAILY_BAR_PARTITION.fullmatch(name) is not None
    )
