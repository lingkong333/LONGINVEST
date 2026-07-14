"""Protect migration metadata from the application role."""

import re
from collections.abc import Sequence

from alembic import op
from long_invest.platform.config.settings import get_settings

revision: str = "20260714_0003"
down_revision: str | None = "20260714_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$", re.ASCII)


def _application_role() -> str:
    role = get_settings().database_app_role
    if ROLE_PATTERN.fullmatch(role) is None:
        raise ValueError("unsafe PostgreSQL identifier")
    return f'"{role}"'


def upgrade() -> None:
    role = _application_role()
    op.execute(f"REVOKE ALL ON TABLE alembic_version FROM {role}")
    op.execute(f"GRANT SELECT ON TABLE alembic_version TO {role}")


def downgrade() -> None:
    role = _application_role()
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE alembic_version TO {role}"
    )
