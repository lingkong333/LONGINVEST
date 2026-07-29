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


def include_schema_object(
    object_: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object | None,
) -> bool:
    """Ignore inherited child-partition foreign keys reported by PostgreSQL."""
    del name, compare_to
    if type_ != "foreign_key_constraint" or not reflected:
        return True

    table = getattr(object_, "table", None)
    if getattr(table, "name", None) != "daily_bar_revision":
        return True

    for element in getattr(object_, "elements", ()):
        target = getattr(element, "target_fullname", "")
        target_table = target.rsplit(".", 1)[0].rsplit(".", 1)[-1]
        if _DAILY_BAR_PARTITION.fullmatch(target_table) is not None:
            return False
    return True
