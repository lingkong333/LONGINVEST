"""Separate the low-privilege application database role."""

import re
from collections.abc import Sequence

from alembic import op
from long_invest.platform.config.settings import get_settings

revision: str = "20260714_0002"
down_revision: str | None = "20260714_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$", re.ASCII)


def _quoted_identifier(value: str) -> str:
    if ROLE_PATTERN.fullmatch(value) is None:
        raise ValueError("unsafe PostgreSQL identifier")
    return f'"{value}"'


def upgrade() -> None:
    bind = op.get_bind()
    role = _quoted_identifier(get_settings().database_app_role)
    database = bind.dialect.identifier_preparer.quote_identifier(
        bind.exec_driver_sql("SELECT current_database()").scalar_one()
    )

    op.execute(f"GRANT CONNECT ON DATABASE {database} TO {role}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}")
    op.execute(f"REVOKE ALL ON TABLE audit_event FROM {role}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE audit_event TO {role}")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {role}"
    )


def downgrade() -> None:
    role = _quoted_identifier(get_settings().database_app_role)
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {role}"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE USAGE, SELECT ON SEQUENCES FROM {role}"
    )
    op.execute(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {role}")
    op.execute(f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {role}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {role}")
