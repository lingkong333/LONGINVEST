"""Enable the Sina security-master fallback.

Revision ID: 20260723_0025
Revises: 20260723_0024
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260723_0025"
down_revision: str | None = "20260723_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EASTMONEY_CONFIG_ID = "10000000-0000-0000-0000-000000000025"
SINA_CONFIG_ID = "10000000-0000-0000-0000-000000000026"


def upgrade() -> None:
    op.execute(
        f"""
        WITH next_version AS (
            SELECT COALESCE(MAX(version), 0) + 1 AS version
            FROM provider_config_version
            WHERE provider_code = 'eastmoney'
        ),
        inserted AS (
            INSERT INTO provider_config_version
                (id, version, provider_code, reason)
            SELECT
                '{EASTMONEY_CONFIG_ID}',
                version,
                'eastmoney',
                'enable security master fallback'
            FROM next_version
            RETURNING version
        )
        INSERT INTO provider_capability_setting
            (id, config_version, provider_code, capability, enabled, priority,
             concurrency, rate_per_second, timeout_seconds, auto_switch)
        SELECT route_values.id::uuid, inserted.version, 'eastmoney',
               route_values.capability, TRUE, 0, 2, 2.0, 5.0,
               route_values.auto_switch
        FROM inserted
        CROSS JOIN (
            VALUES
                ('25000000-0000-0000-0000-000000000001',
                 'SECURITY_MASTER', TRUE),
                ('25000000-0000-0000-0000-000000000002',
                 'REALTIME_QUOTE_BATCH', TRUE),
                ('25000000-0000-0000-0000-000000000003',
                 'DAILY_BAR_UNADJUSTED', FALSE),
                ('25000000-0000-0000-0000-000000000004',
                 'HISTORICAL_DAILY_UNADJUSTED', FALSE),
                ('25000000-0000-0000-0000-000000000005',
                 'HISTORICAL_DAILY_QFQ', FALSE),
                ('25000000-0000-0000-0000-000000000006',
                 'CORPORATE_ACTIONS', FALSE)
        ) AS route_values(id, capability, auto_switch)
        """
    )
    op.execute(
        f"""
        WITH next_version AS (
            SELECT COALESCE(MAX(version), 0) + 1 AS version
            FROM provider_config_version
            WHERE provider_code = 'sina'
        ),
        inserted AS (
            INSERT INTO provider_config_version
                (id, version, provider_code, reason)
            SELECT
                '{SINA_CONFIG_ID}',
                version,
                'sina',
                'add low frequency security master fallback'
            FROM next_version
            RETURNING version
        )
        INSERT INTO provider_capability_setting
            (id, config_version, provider_code, capability, enabled, priority,
             concurrency, rate_per_second, timeout_seconds, auto_switch)
        SELECT route_values.id::uuid, inserted.version, 'sina',
               route_values.capability, TRUE, route_values.priority,
               2, 1.0, 180.0, FALSE
        FROM inserted
        CROSS JOIN (
            VALUES
                ('25000000-0000-0000-0000-000000000007',
                 'SECURITY_MASTER', 1),
                ('25000000-0000-0000-0000-000000000008',
                 'REALTIME_QUOTE_BATCH', 1)
        ) AS route_values(id, capability, priority)
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE provider_capability_setting "
        "DISABLE TRIGGER provider_capability_setting_append_only"
    )
    op.execute(
        "ALTER TABLE provider_config_version "
        "DISABLE TRIGGER provider_config_version_append_only"
    )
    for provider, config_id in (
        ("eastmoney", EASTMONEY_CONFIG_ID),
        ("sina", SINA_CONFIG_ID),
    ):
        op.execute(
            f"""
            DELETE FROM provider_capability_setting
            WHERE provider_code = '{provider}'
              AND config_version = (
                  SELECT version FROM provider_config_version
                  WHERE id = '{config_id}'
              )
            """
        )
        op.execute(
            f"DELETE FROM provider_config_version WHERE id = '{config_id}'"
        )
    op.execute(
        "ALTER TABLE provider_config_version "
        "ENABLE TRIGGER provider_config_version_append_only"
    )
    op.execute(
        "ALTER TABLE provider_capability_setting "
        "ENABLE TRIGGER provider_capability_setting_append_only"
    )
