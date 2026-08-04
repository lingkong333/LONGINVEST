"""Allow the full-market daily batch to switch providers on failure.

Revision ID: 20260804_0045
Revises: 20260804_0044
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260804_0045"
down_revision: str | None = "20260804_0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONFIG_ID = "10000000-0000-0000-0000-000000000045"


def upgrade() -> None:
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
            SELECT '{CONFIG_ID}', latest.version + 1, 'EASTMONEY',
                   'allow full-market daily batch provider fallback'
            FROM latest
            RETURNING version
        )
        INSERT INTO provider_capability_setting
            (id, config_version, provider_code, capability, enabled, priority,
             concurrency, rate_per_second, timeout_seconds, auto_switch,
             daily_limit, min_interval_seconds)
        SELECT gen_random_uuid(), inserted.version, 'EASTMONEY', current.capability,
               current.enabled, current.priority, current.concurrency,
               current.rate_per_second, current.timeout_seconds,
               CASE WHEN current.capability = 'DAILY_BAR_UNADJUSTED'
                    THEN TRUE ELSE current.auto_switch END,
               current.daily_limit, current.min_interval_seconds
        FROM inserted
        CROSS JOIN latest
        JOIN provider_capability_setting AS current
          ON current.provider_code = 'EASTMONEY'
         AND current.config_version = latest.version
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
    op.execute(
        f"""
        DELETE FROM provider_capability_setting
        WHERE provider_code = 'EASTMONEY'
          AND config_version = (
              SELECT version FROM provider_config_version WHERE id = '{CONFIG_ID}'
          )
        """
    )
    op.execute(f"DELETE FROM provider_config_version WHERE id = '{CONFIG_ID}'")
    op.execute(
        "ALTER TABLE provider_config_version "
        "ENABLE TRIGGER provider_config_version_append_only"
    )
    op.execute(
        "ALTER TABLE provider_capability_setting "
        "ENABLE TRIGGER provider_capability_setting_append_only"
    )
