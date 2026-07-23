"""Normalize provider codes to the public contract.

Revision ID: 20260723_0023
Revises: 20260723_0022
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260723_0023"
down_revision: str | None = "20260723_0022"
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
IMMUTABLE_PROVIDER_TABLES = (
    "provider_config_version",
    "provider_capability_setting",
    "provider_circuit_history",
)
SUPPORTED_CODES = "'EASTMONEY', 'SINA'"


def _set_immutable_triggers(*, enabled: bool) -> None:
    action = "ENABLE" if enabled else "DISABLE"
    for table_name in IMMUTABLE_PROVIDER_TABLES:
        op.execute(f"ALTER TABLE {table_name} {action} TRIGGER USER")


def upgrade() -> None:
    _set_immutable_triggers(enabled=False)
    for table_name in PROVIDER_TABLES:
        op.execute(
            f"""
            UPDATE {table_name}
            SET provider_code = upper(provider_code)
            WHERE upper(provider_code) IN ({SUPPORTED_CODES})
              AND provider_code <> upper(provider_code)
            """
        )
    _set_immutable_triggers(enabled=True)

    for table_name in PROVIDER_TABLES:
        op.create_check_constraint(
            op.f(f"ck_{table_name}_provider_code_supported"),
            table_name,
            f"provider_code IN ({SUPPORTED_CODES})",
        )


def downgrade() -> None:
    for table_name in reversed(PROVIDER_TABLES):
        op.drop_constraint(
            op.f(f"ck_{table_name}_provider_code_supported"),
            table_name,
            type_="check",
        )

    _set_immutable_triggers(enabled=False)
    for table_name in PROVIDER_TABLES:
        op.execute(
            f"""
            UPDATE {table_name}
            SET provider_code = lower(provider_code)
            WHERE provider_code IN ({SUPPORTED_CODES})
            """
        )
    _set_immutable_triggers(enabled=True)
