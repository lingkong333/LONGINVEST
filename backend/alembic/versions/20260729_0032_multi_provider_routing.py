"""Add probe-gated multi-provider routing metadata.

Revision ID: 20260729_0032
Revises: 20260729_0031
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260729_0032"
down_revision: str | None = "20260729_0031"
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


def upgrade() -> None:
    for table_name in PROVIDER_TABLES:
        op.drop_constraint(
            op.f(f"ck_{table_name}_provider_code_supported"),
            table_name,
            type_="check",
        )
        op.create_check_constraint(
            op.f(f"ck_{table_name}_provider_code_supported"),
            table_name,
            "provider_code IN ('EASTMONEY','SINA','TUSHARE','BAOSTOCK')",
        )

    op.create_table(
        "provider_capability_registration",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_code", sa.String(32), nullable=False),
        sa.Column("capability", sa.String(64), nullable=False),
        sa.Column("adapter_code", sa.String(32), nullable=False),
        sa.Column("interface_code", sa.String(500), nullable=False),
        sa.Column("algorithm_version", sa.String(64), nullable=False),
        sa.Column("credential_ref", sa.String(255), nullable=True),
        sa.Column("probe_status", sa.String(16), nullable=False),
        sa.Column("last_probe_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "probe_status IN ('UNKNOWN','PASSED','FAILED')",
            name=op.f("ck_provider_capability_registration_probe_status_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_capability_registration")),
        sa.UniqueConstraint(
            "provider_code",
            "capability",
            name=op.f("uq_provider_capability_registration_provider_code"),
        ),
    )
    op.create_table(
        "provider_route_policy_version",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("capability", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("fixed_provider_code", sa.String(32), nullable=True),
        sa.Column("reason", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_route_policy_version")),
        sa.UniqueConstraint(
            "capability",
            "version",
            name=op.f("uq_provider_route_policy_version_capability"),
        ),
    )
    op.execute(
        """
        INSERT INTO provider_capability_registration
            (id, provider_code, capability, adapter_code, interface_code,
             algorithm_version, probe_status, last_probe_at)
        SELECT gen_random_uuid(), s.provider_code, s.capability,
               CASE WHEN s.provider_code = 'SINA'
                          AND s.capability LIKE 'HISTORICAL_DAILY_%'
                    THEN 'AKSHARE' ELSE 'HTTPX' END,
               CASE
                 WHEN s.provider_code = 'EASTMONEY' AND s.capability = 'SECURITY_MASTER'
                   THEN 'https://push2.eastmoney.com/api/qt/clist/get'
                 WHEN s.provider_code = 'EASTMONEY'
                      AND s.capability = 'REALTIME_QUOTE_BATCH'
                   THEN 'https://push2.eastmoney.com/api/qt/ulist.np/get'
                 WHEN s.provider_code = 'EASTMONEY' AND s.capability LIKE '%DAILY%'
                   THEN 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
                 WHEN s.provider_code = 'EASTMONEY'
                      AND s.capability = 'CORPORATE_ACTIONS'
                   THEN 'https://datacenter-web.eastmoney.com/api/data/v1/get'
                 WHEN s.provider_code = 'SINA'
                      AND s.capability LIKE 'HISTORICAL_DAILY_%'
                   THEN 'akshare.stock_zh_a_hist'
                 WHEN s.provider_code = 'SINA' AND s.capability = 'REALTIME_QUOTE_BATCH'
                   THEN 'https://hq.sinajs.cn/list='
                 ELSE 'Market_Center.getHQNodeData'
               END,
               CASE WHEN s.capability = 'HISTORICAL_DAILY_QFQ'
                    THEN lower(s.provider_code) || '-qfq-v1' ELSE 'raw-v1' END,
               'PASSED', now()
        FROM (
            SELECT DISTINCT provider_code, capability
            FROM provider_capability_setting
        ) AS s
        """
    )
    op.execute(
        "CREATE TRIGGER provider_route_policy_version_append_only "
        "BEFORE UPDATE OR DELETE ON provider_route_policy_version "
        "FOR EACH ROW EXECUTE FUNCTION reject_stage2_fact_mutation()"
    )
    op.add_column(
        "daily_bar_unadjusted",
        sa.Column("source_identity", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "daily_bar_unadjusted",
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE daily_bar_unadjusted
        SET source_identity = jsonb_build_object(
                'adapter', 'LEGACY', 'upstream', source,
                'interface', 'legacy-daily-bar',
                'capability', 'DAILY_BAR_UNADJUSTED',
                'algorithm_version', 'raw-v1'
            ),
            collected_at = updated_at
        """
    )
    op.alter_column("daily_bar_unadjusted", "source_identity", nullable=False)
    op.alter_column("daily_bar_unadjusted", "collected_at", nullable=False)
    op.add_column(
        "quote_cycle_item",
        sa.Column("source_identity", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "security",
        sa.Column("source_identity", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "security_master_version",
        sa.Column("source_identity", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("security_master_version", "source_identity")
    op.drop_column("security", "source_identity")
    op.drop_column("quote_cycle_item", "source_identity")
    op.drop_column("daily_bar_unadjusted", "collected_at")
    op.drop_column("daily_bar_unadjusted", "source_identity")
    op.execute(
        "DROP TRIGGER IF EXISTS provider_route_policy_version_append_only "
        "ON provider_route_policy_version"
    )
    op.drop_table("provider_route_policy_version")
    op.drop_table("provider_capability_registration")
    for table_name in PROVIDER_TABLES:
        op.drop_constraint(
            op.f(f"ck_{table_name}_provider_code_supported"),
            table_name,
            type_="check",
        )
        op.create_check_constraint(
            op.f(f"ck_{table_name}_provider_code_supported"),
            table_name,
            "provider_code IN ('EASTMONEY','SINA')",
        )
