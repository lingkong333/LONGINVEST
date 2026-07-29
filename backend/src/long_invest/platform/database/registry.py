from collections.abc import Mapping
from functools import lru_cache
from importlib import import_module
from pkgutil import walk_packages
from types import MappingProxyType, ModuleType

from sqlalchemy import Table

import long_invest.modules as business_modules
import long_invest.platform as platform_modules
from long_invest.platform.database.base import Base

_MODEL_ROOTS = (business_modules, platform_modules)


@lru_cache
def load_model_modules() -> tuple[str, ...]:
    names: list[str] = []
    for root in _MODEL_ROOTS:
        names.extend(_model_module_names(root))
    for name in sorted(set(names)):
        import_module(name)
    return tuple(sorted(set(names)))


@lru_cache
def table_ownership() -> Mapping[str, str]:
    load_model_modules()
    owners: dict[str, str] = {}
    for mapper in Base.registry.mappers:
        table = mapper.local_table
        if not isinstance(table, Table):
            raise RuntimeError(f"mapped class {mapper.class_.__name__} has no table")
        owner = _owner_from_module(mapper.class_.__module__)
        existing = owners.setdefault(table.fullname, owner)
        if existing != owner:
            raise RuntimeError(
                f"table {table.fullname} is owned by both {existing} and {owner}"
            )

    metadata_tables = set(Base.metadata.tables)
    registered_tables = set(owners)
    if metadata_tables != registered_tables:
        missing = sorted(metadata_tables - registered_tables)
        unknown = sorted(registered_tables - metadata_tables)
        raise RuntimeError(
            f"table ownership is incomplete: missing={missing}, unknown={unknown}"
        )
    return MappingProxyType(dict(sorted(owners.items())))


def _model_module_names(root: ModuleType) -> tuple[str, ...]:
    return tuple(
        item.name
        for item in walk_packages(root.__path__, prefix=f"{root.__name__}.")
        if item.name.endswith(".models")
    )


def _owner_from_module(module_name: str) -> str:
    parts = module_name.split(".")
    if len(parts) < 4 or parts[0] != "long_invest":
        raise RuntimeError(f"model module has no LongInvest owner: {module_name}")
    if parts[1] not in {"modules", "platform"}:
        raise RuntimeError(f"model module has no supported owner: {module_name}")
    return f"{parts[1]}.{parts[2]}"
