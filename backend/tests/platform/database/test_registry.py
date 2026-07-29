from long_invest.platform.database.base import Base
from long_invest.platform.database.registry import (
    load_model_modules,
    table_ownership,
)


def test_all_model_tables_have_exactly_one_module_owner() -> None:
    owners = table_ownership()

    assert set(owners) == set(Base.metadata.tables)
    assert all(owner.startswith(("modules.", "platform.")) for owner in owners.values())
    assert owners["app_user"] == "modules.auth"
    assert owners["daily_bar_unadjusted"] == "modules.daily_data"
    assert owners["job"] == "platform.jobs"
    assert owners["audit_event"] == "platform.audit"


def test_model_discovery_includes_business_and_platform_tables() -> None:
    modules = load_model_modules()

    assert "long_invest.modules.auth.models" in modules
    assert "long_invest.modules.daily_data.models" in modules
    assert "long_invest.platform.audit.models" in modules
    assert "long_invest.platform.jobs.models" in modules
