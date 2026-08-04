"""Register Sina's validated batch quote interface for daily bars.

Revision ID: 20260804_0046
Revises: 20260804_0045
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260804_0046"
down_revision: str | None = "20260804_0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO provider_capability_registration
            (id, provider_code, capability, adapter_code, interface_code,
             algorithm_version, probe_status, last_probe_at)
        VALUES
            (gen_random_uuid(), 'SINA', 'DAILY_BAR_UNADJUSTED', 'HTTPX',
             'https://hq.sinajs.cn/list=', 'raw-v1', 'PASSED', now())
        ON CONFLICT (provider_code, capability) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM provider_capability_registration "
        "WHERE provider_code = 'SINA' AND capability = 'DAILY_BAR_UNADJUSTED'"
    )
