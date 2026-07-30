"""Register Tushare and BaoStock adapters.

Revision ID: 20260730_0033
Revises: 20260729_0032
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260730_0033"
down_revision: str | None = "20260729_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CAPABILITIES = (
    "SECURITY_MASTER",
    "DAILY_BAR_UNADJUSTED",
    "HISTORICAL_DAILY_UNADJUSTED",
    "HISTORICAL_DAILY_QFQ",
)


def upgrade() -> None:
    for provider, adapter in (
        ("TUSHARE", "TUSHARE_SDK"),
        ("BAOSTOCK", "BAOSTOCK_SDK"),
    ):
        op.execute(
            f"""
            INSERT INTO provider_config_version
                (id, version, provider_code, reason)
            VALUES
                (gen_random_uuid(), 1, '{provider}', 'register provider adapter')
            """
        )
        op.execute(
            f"""
            INSERT INTO provider_budget_policy
                (id, config_version, provider_code, daily_limit, reset_timezone,
                 max_concurrency, realtime_reserved, daily_reserved)
            VALUES
                (gen_random_uuid(), 1, '{provider}', 50000, 'Asia/Shanghai',
                 4, 0, 500)
            """
        )
        for priority, capability in enumerate(CAPABILITIES, start=20):
            interface = (
                "tushare.pro.stock_basic"
                if provider == "TUSHARE" and capability == "SECURITY_MASTER"
                else (
                    "tushare.pro_bar"
                    if provider == "TUSHARE" and capability == "HISTORICAL_DAILY_QFQ"
                    else (
                        "tushare.pro.daily"
                        if provider == "TUSHARE"
                        else (
                            "baostock.query_all_stock"
                            if capability == "SECURITY_MASTER"
                            else "baostock.query_history_k_data_plus"
                        )
                    )
                )
            )
            algorithm = (
                f"{provider.lower()}-qfq-v1"
                if capability == "HISTORICAL_DAILY_QFQ"
                else "raw-v1"
            )
            credential_ref = (
                "'secret://provider.tushare.token'" if provider == "TUSHARE" else "NULL"
            )
            op.execute(
                f"""
                INSERT INTO provider_capability_setting
                    (id, config_version, provider_code, capability, enabled,
                     priority, concurrency, rate_per_second, timeout_seconds,
                     auto_switch, daily_limit, min_interval_seconds)
                VALUES
                    (gen_random_uuid(), 1, '{provider}', '{capability}', false,
                     {priority}, 1, 1, 30, true, 50000, 1)
                """
            )
            op.execute(
                f"""
                INSERT INTO provider_capability_registration
                    (id, provider_code, capability, adapter_code, interface_code,
                     algorithm_version, credential_ref, probe_status, last_probe_at)
                VALUES
                    (gen_random_uuid(), '{provider}', '{capability}', '{adapter}',
                     '{interface}', '{algorithm}', {credential_ref}, 'UNKNOWN', NULL)
                """
            )


def downgrade() -> None:
    immutable_tables = (
        "provider_config_version",
        "provider_capability_setting",
        "provider_budget_policy",
    )
    for table in immutable_tables:
        op.execute(f"ALTER TABLE {table} DISABLE TRIGGER {table}_append_only")
    op.execute(
        "DELETE FROM provider_capability_registration "
        "WHERE provider_code IN ('TUSHARE','BAOSTOCK')"
    )
    op.execute(
        "DELETE FROM provider_capability_setting "
        "WHERE provider_code IN ('TUSHARE','BAOSTOCK')"
    )
    op.execute(
        "DELETE FROM provider_budget_policy "
        "WHERE provider_code IN ('TUSHARE','BAOSTOCK')"
    )
    op.execute(
        "DELETE FROM provider_config_version "
        "WHERE provider_code IN ('TUSHARE','BAOSTOCK')"
    )
    for table in immutable_tables:
        op.execute(f"ALTER TABLE {table} ENABLE TRIGGER {table}_append_only")
