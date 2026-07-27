"""Route complete historical daily bars through Sina.

Revision ID: 20260727_0026
Revises: 20260723_0025
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260727_0026"
down_revision: str | None = "20260723_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EASTMONEY_CONFIG_ID = "10000000-0000-0000-0000-000000000027"
SINA_CONFIG_ID = "10000000-0000-0000-0000-000000000028"


def upgrade() -> None:
    _copy_eastmoney_config()
    _copy_sina_config_with_history()


def _copy_eastmoney_config() -> None:
    op.execute(
        f"""
        WITH latest AS (
            SELECT MAX(version) AS version
            FROM provider_config_version
            WHERE provider_code = 'EASTMONEY'
        ),
        inserted AS (
            INSERT INTO provider_config_version
                (id, version, provider_code, reason)
            SELECT '{EASTMONEY_CONFIG_ID}', latest.version + 1, 'EASTMONEY',
                   'retain eastmoney as manual history fallback'
            FROM latest
            RETURNING version
        )
        INSERT INTO provider_capability_setting
            (id, config_version, provider_code, capability, enabled, priority,
             concurrency, rate_per_second, timeout_seconds, auto_switch)
        SELECT gen_random_uuid(), inserted.version, 'EASTMONEY', current.capability,
               current.enabled,
               CASE WHEN current.capability LIKE 'HISTORICAL_DAILY_%' THEN 1
                    ELSE current.priority END,
               current.concurrency, current.rate_per_second,
               current.timeout_seconds,
               CASE WHEN current.capability LIKE 'HISTORICAL_DAILY_%' THEN FALSE
                    ELSE current.auto_switch END
        FROM inserted
        CROSS JOIN latest
        JOIN provider_capability_setting AS current
          ON current.provider_code = 'EASTMONEY'
         AND current.config_version = latest.version
        """
    )


def _copy_sina_config_with_history() -> None:
    op.execute(
        f"""
        WITH latest AS (
            SELECT MAX(version) AS version
            FROM provider_config_version
            WHERE provider_code = 'SINA'
        ),
        inserted AS (
            INSERT INTO provider_config_version
                (id, version, provider_code, reason)
            SELECT '{SINA_CONFIG_ID}', latest.version + 1, 'SINA',
                   'enable low frequency complete history'
            FROM latest
            RETURNING version
        ),
        existing AS (
            SELECT current.capability, current.enabled, current.priority,
                   current.concurrency, current.rate_per_second,
                   current.timeout_seconds, current.auto_switch
            FROM latest
            JOIN provider_capability_setting AS current
              ON current.provider_code = 'SINA'
             AND current.config_version = latest.version
        ),
        wanted AS (
            SELECT * FROM existing
            UNION ALL
            SELECT capability, TRUE, 0, 1, 0.333333, 300.0, FALSE
            FROM (VALUES
                ('HISTORICAL_DAILY_UNADJUSTED'),
                ('HISTORICAL_DAILY_QFQ')
            ) AS history(capability)
        )
        INSERT INTO provider_capability_setting
            (id, config_version, provider_code, capability, enabled, priority,
             concurrency, rate_per_second, timeout_seconds, auto_switch)
        SELECT gen_random_uuid(), inserted.version, 'SINA', wanted.capability,
               wanted.enabled, wanted.priority, wanted.concurrency,
               wanted.rate_per_second, wanted.timeout_seconds, wanted.auto_switch
        FROM inserted
        CROSS JOIN wanted
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
    for config_id in (SINA_CONFIG_ID, EASTMONEY_CONFIG_ID):
        op.execute(
            f"""
            DELETE FROM provider_capability_setting
            WHERE config_version = (
                SELECT version FROM provider_config_version WHERE id = '{config_id}'
            )
              AND provider_code = (
                SELECT provider_code FROM provider_config_version
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
