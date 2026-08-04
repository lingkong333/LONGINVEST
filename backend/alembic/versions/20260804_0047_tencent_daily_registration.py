"""Register Tencent's batch quote interface for current-day daily bars.

Revision ID: 20260804_0047
Revises: 20260804_0046
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260804_0047"
down_revision: str | None = "20260804_0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO provider_capability_setting
            (id, config_version, provider_code, capability, enabled, priority,
             concurrency, rate_per_second, timeout_seconds, auto_switch,
             daily_limit, min_interval_seconds)
        VALUES
            (gen_random_uuid(), 1, 'TENCENT', 'DAILY_BAR_UNADJUSTED', true, 3,
             4, 2, 30, true, 50000, 0.5)
        """
    )
    op.execute(
        """
        INSERT INTO provider_capability_registration
            (id, provider_code, capability, adapter_code, interface_code,
             algorithm_version, probe_status, last_probe_at)
        VALUES
            (gen_random_uuid(), 'TENCENT', 'DAILY_BAR_UNADJUSTED', 'HTTPX',
             'https://qt.gtimg.cn/q=', 'raw-v1', 'PASSED', now())
        ON CONFLICT (provider_code, capability) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE provider_capability_setting "
        "DISABLE TRIGGER provider_capability_setting_append_only"
    )
    op.execute(
        "DELETE FROM provider_capability_registration "
        "WHERE provider_code = 'TENCENT' "
        "AND capability = 'DAILY_BAR_UNADJUSTED'"
    )
    op.execute(
        "DELETE FROM provider_capability_setting "
        "WHERE provider_code = 'TENCENT' "
        "AND capability = 'DAILY_BAR_UNADJUSTED'"
    )
    op.execute(
        "ALTER TABLE provider_capability_setting "
        "ENABLE TRIGGER provider_capability_setting_append_only"
    )
