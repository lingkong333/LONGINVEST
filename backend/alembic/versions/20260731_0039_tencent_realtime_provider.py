"""Register Tencent as a realtime quote fallback.

Revision ID: 20260731_0039
Revises: 20260731_0038
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260731_0039"
down_revision: str | None = "20260731_0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROVIDER_TABLES = (
    "provider_config_version",
    "provider_capability_setting",
    "provider_health_state",
    "provider_circuit_history",
    "provider_circuit_state",
    "provider_failure_sample",
)


def _provider_constraint(values: str) -> None:
    for table_name in PROVIDER_TABLES:
        op.drop_constraint(
            op.f(f"ck_{table_name}_provider_code_supported"),
            table_name,
            type_="check",
        )
        op.create_check_constraint(
            op.f(f"ck_{table_name}_provider_code_supported"),
            table_name,
            f"provider_code IN ({values})",
        )


def upgrade() -> None:
    _provider_constraint("'EASTMONEY','SINA','TUSHARE','BAOSTOCK','TENCENT'")
    op.execute(
        """
        INSERT INTO provider_config_version (id, version, provider_code, reason)
        VALUES (gen_random_uuid(), 1, 'TENCENT', 'register realtime fallback')
        """
    )
    op.execute(
        """
        INSERT INTO provider_budget_policy
            (id, config_version, provider_code, daily_limit, reset_timezone,
             max_concurrency, realtime_reserved, daily_reserved)
        VALUES
            (gen_random_uuid(), 1, 'TENCENT', 50000, 'Asia/Shanghai', 4, 500, 0)
        """
    )
    op.execute(
        """
        INSERT INTO provider_capability_setting
            (id, config_version, provider_code, capability, enabled, priority,
             concurrency, rate_per_second, timeout_seconds, auto_switch,
             daily_limit, min_interval_seconds)
        VALUES
            (gen_random_uuid(), 1, 'TENCENT', 'REALTIME_QUOTE_BATCH', true, 3,
             4, 2, 30, true, 50000, 0.5)
        """
    )
    op.execute(
        """
        INSERT INTO provider_capability_registration
            (id, provider_code, capability, adapter_code, interface_code,
             algorithm_version, probe_status, last_probe_at)
        VALUES
            (gen_random_uuid(), 'TENCENT', 'REALTIME_QUOTE_BATCH', 'HTTPX',
             'https://qt.gtimg.cn/q=', 'raw-v1', 'PASSED', now())
        """
    )


def downgrade() -> None:
    immutable_tables = (
        "provider_config_version",
        "provider_capability_setting",
        "provider_budget_policy",
    )
    for table_name in immutable_tables:
        op.execute(f"ALTER TABLE {table_name} DISABLE TRIGGER {table_name}_append_only")
    op.execute(
        "DELETE FROM provider_capability_registration WHERE provider_code = 'TENCENT'"
    )
    op.execute(
        "DELETE FROM provider_capability_setting WHERE provider_code = 'TENCENT'"
    )
    op.execute("DELETE FROM provider_budget_policy WHERE provider_code = 'TENCENT'")
    op.execute("DELETE FROM provider_config_version WHERE provider_code = 'TENCENT'")
    for table_name in immutable_tables:
        op.execute(f"ALTER TABLE {table_name} ENABLE TRIGGER {table_name}_append_only")
    _provider_constraint("'EASTMONEY','SINA','TUSHARE','BAOSTOCK'")
