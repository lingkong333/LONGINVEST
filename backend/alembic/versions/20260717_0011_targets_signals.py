"""Create target and signal storage.

Revision ID: 20260717_0011
Revises: 20260716_0010
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from long_invest.platform.config.settings import get_settings

revision: str = "20260717_0011"
down_revision: str | None = "20260716_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
TABLES = (
    "target_revision",
    "subscription_target_binding",
    "signal_state",
    "signal_evaluation",
    "signal_event",
)
IMMUTABLE_TABLES = (
    "target_revision",
    "signal_evaluation",
    "signal_event",
)
MUTABLE_TABLES = (
    "subscription_target_binding",
    "signal_state",
)

ZONES = "'UNKNOWN','STRONG_LOW','LOW','NORMAL','HIGH','STRONG_HIGH'"
REASONS = (
    "'SCHEDULED_QUOTE','MANUAL_CHECK','TARGET_ACTIVATED',"
    "'POSITION_BECAME_HOLDING','DATA_CORRECTION','STATE_RESET',"
    "'RECOVERY_REEVALUATION'"
)


def _application_role() -> str:
    role = get_settings().database_app_role
    if ROLE_PATTERN.fullmatch(role) is None:
        raise ValueError("unsafe PostgreSQL identifier")
    return f'"{role}"'


def _protect_immutable_facts() -> None:
    op.execute(
        """
        CREATE FUNCTION reject_target_signal_fact_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'target and signal historical facts are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table_name in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table_name}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table_name} "
            "FOR EACH ROW EXECUTE FUNCTION reject_target_signal_fact_mutation()"
        )


def upgrade() -> None:
    op.create_table(
        "target_revision",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("subscription_id", sa.UUID(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("low_strong", sa.Numeric(20, 2), nullable=False),
        sa.Column("low_watch", sa.Numeric(20, 2), nullable=False),
        sa.Column("high_watch", sa.Numeric(20, 2), nullable=False),
        sa.Column("high_strong", sa.Numeric(20, 2), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("source_revision_id", sa.UUID(), nullable=True),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("strategy_version_id", sa.UUID(), nullable=True),
        sa.Column(
            "parameter_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("data_version", sa.Integer(), nullable=True),
        sa.Column("source_code_hash", sa.String(64), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("large_change_confirmed", sa.Boolean(), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("actor_user_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("trusted_ip", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "revision_no > 0",
            name=op.f("ck_target_revision_revision_positive"),
        ),
        sa.CheckConstraint(
            "source IN ('MANUAL','RESTORED')",
            name=op.f("ck_target_revision_source_valid"),
        ),
        sa.CheckConstraint(
            "(source = 'RESTORED' AND source_revision_id IS NOT NULL) OR "
            "(source = 'MANUAL' AND source_revision_id IS NULL)",
            name=op.f("ck_target_revision_source_revision_consistent"),
        ),
        sa.CheckConstraint(
            "low_strong > 0 AND low_watch > 0 "
            "AND high_watch > 0 AND high_strong > 0 "
            "AND low_strong <> 'NaN'::numeric "
            "AND low_watch <> 'NaN'::numeric "
            "AND high_watch <> 'NaN'::numeric "
            "AND high_strong <> 'NaN'::numeric "
            "AND low_strong < 'Infinity'::numeric "
            "AND low_watch < 'Infinity'::numeric "
            "AND high_watch < 'Infinity'::numeric "
            "AND high_strong < 'Infinity'::numeric "
            "AND low_strong < low_watch AND low_watch < high_watch "
            "AND high_watch < high_strong",
            name=op.f("ck_target_revision_values_ordered"),
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name=op.f("ck_target_revision_content_hash_sha256"),
        ),
        sa.CheckConstraint(
            "source_code_hash IS NULL OR length(source_code_hash) = 64",
            name=op.f("ck_target_revision_source_code_hash_sha256"),
        ),
        sa.CheckConstraint(
            "data_version IS NULL OR data_version > 0",
            name=op.f("ck_target_revision_data_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["monitor_subscription.id"],
            name=op.f("fk_target_revision_subscription_id_monitor_subscription"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_revision_id"],
            ["target_revision.id"],
            name=op.f("fk_target_revision_source_revision_id_target_revision"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_target_revision")),
        sa.UniqueConstraint(
            "subscription_id",
            "revision_no",
            name="uq_target_revision_revision_number",
        ),
        sa.UniqueConstraint(
            "subscription_id",
            "idempotency_key",
            name="uq_target_revision_idempotency",
        ),
    )
    op.create_index(
        "ix_target_revision_subscription_created",
        "target_revision",
        ["subscription_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "subscription_target_binding",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("subscription_id", sa.UUID(), nullable=False),
        sa.Column("current_revision_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stale_reason", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version > 0",
            name=op.f("ck_subscription_target_binding_version_positive"),
        ),
        sa.CheckConstraint(
            "status IN ('READY','STALE','CALCULATING','REVIEW_REQUIRED',"
            "'ACTIVATING','FAILED','MISSING')",
            name=op.f("ck_subscription_target_binding_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["monitor_subscription.id"],
            name=op.f(
                "fk_subscription_target_binding_subscription_id_monitor_subscription"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["current_revision_id"],
            ["target_revision.id"],
            name=op.f(
                "fk_subscription_target_binding_current_revision_id_target_revision"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscription_target_binding")),
        sa.UniqueConstraint(
            "subscription_id",
            name="uq_subscription_target_binding_subscription",
        ),
    )
    op.create_index(
        "ix_subscription_target_binding_status",
        "subscription_target_binding",
        ["status"],
        unique=False,
    )

    op.create_table(
        "signal_state",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("subscription_id", sa.UUID(), nullable=False),
        sa.Column("zone", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("last_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("last_price_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_subscription_version", sa.Integer(), nullable=True),
        sa.Column("last_price_version", sa.Integer(), nullable=True),
        sa.Column("last_quote_cycle_id", sa.UUID(), nullable=True),
        sa.Column(
            "last_quote_scheduled_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("last_quote_item_id", sa.UUID(), nullable=True),
        sa.Column("last_target_revision_id", sa.UUID(), nullable=True),
        sa.Column("last_target_version", sa.Integer(), nullable=True),
        sa.Column("last_position_version", sa.Integer(), nullable=True),
        sa.Column("last_evaluation_id", sa.UUID(), nullable=True),
        sa.Column("last_event_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"zone IN ({ZONES})", name=op.f("ck_signal_state_zone_valid")
        ),
        sa.CheckConstraint(
            "version > 0", name=op.f("ck_signal_state_version_positive")
        ),
        sa.CheckConstraint(
            "(last_price IS NULL OR (last_price > 0 "
            "AND last_price <> 'NaN'::numeric "
            "AND last_price < 'Infinity'::numeric)) "
            "AND (last_subscription_version IS NULL "
            "OR last_subscription_version > 0) "
            "AND (last_price_version IS NULL OR last_price_version > 0) "
            "AND (last_target_version IS NULL OR last_target_version > 0) "
            "AND (last_position_version IS NULL OR last_position_version >= 0)",
            name=op.f("ck_signal_state_last_inputs_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["monitor_subscription.id"],
            name=op.f("fk_signal_state_subscription_id_monitor_subscription"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["last_target_revision_id"],
            ["target_revision.id"],
            name=op.f("fk_signal_state_last_target_revision_id_target_revision"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signal_state")),
        sa.UniqueConstraint("subscription_id", name="uq_signal_state_subscription"),
    )

    op.create_table(
        "signal_evaluation",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("subscription_id", sa.UUID(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("before_zone", sa.String(16), nullable=False),
        sa.Column("after_zone", sa.String(16), nullable=False),
        sa.Column("subscription_version", sa.Integer(), nullable=True),
        sa.Column("target_revision_id", sa.UUID(), nullable=True),
        sa.Column("target_version", sa.Integer(), nullable=True),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("low_strong", sa.Numeric(20, 2), nullable=True),
        sa.Column("low_watch", sa.Numeric(20, 2), nullable=True),
        sa.Column("high_watch", sa.Numeric(20, 2), nullable=True),
        sa.Column("high_strong", sa.Numeric(20, 2), nullable=True),
        sa.Column("position_status", sa.String(16), nullable=True),
        sa.Column("position_version", sa.Integer(), nullable=True),
        sa.Column("price", sa.Numeric(20, 6), nullable=True),
        sa.Column("price_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("price_version", sa.Integer(), nullable=True),
        sa.Column("quote_cycle_id", sa.UUID(), nullable=True),
        sa.Column("quote_scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quote_item_id", sa.UUID(), nullable=True),
        sa.Column("hysteresis_applied", sa.Boolean(), nullable=False),
        sa.Column("used_stale_target", sa.Boolean(), nullable=False),
        sa.Column("skip_code", sa.String(100), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"reason IN ({REASONS})",
            name=op.f("ck_signal_evaluation_reason_valid"),
        ),
        sa.CheckConstraint(
            "result IN ('APPLIED','UNCHANGED','SKIPPED','SUPERSEDED')",
            name=op.f("ck_signal_evaluation_result_valid"),
        ),
        sa.CheckConstraint(
            f"before_zone IN ({ZONES})",
            name=op.f("ck_signal_evaluation_before_zone_valid"),
        ),
        sa.CheckConstraint(
            f"after_zone IN ({ZONES})",
            name=op.f("ck_signal_evaluation_after_zone_valid"),
        ),
        sa.CheckConstraint(
            "(subscription_version IS NULL OR subscription_version > 0) "
            "AND (price_version IS NULL OR price_version > 0) "
            "AND (target_version IS NULL OR target_version > 0) "
            "AND (position_version IS NULL OR position_version >= 0)",
            name=op.f("ck_signal_evaluation_versions_positive"),
        ),
        sa.CheckConstraint(
            "result IN ('SKIPPED','SUPERSEDED') OR (subscription_version IS NOT NULL "
            "AND target_revision_id IS NOT NULL AND target_version IS NOT NULL "
            "AND target_date IS NOT NULL AND low_strong IS NOT NULL "
            "AND low_watch IS NOT NULL AND high_watch IS NOT NULL "
            "AND high_strong IS NOT NULL AND position_version IS NOT NULL "
            "AND position_status IS NOT NULL AND price IS NOT NULL "
            "AND price_at IS NOT NULL AND price_version IS NOT NULL)",
            name=op.f("ck_signal_evaluation_non_skipped_inputs_complete"),
        ),
        sa.CheckConstraint(
            "price IS NULL OR (price > 0 AND price <> 'NaN'::numeric "
            "AND price < 'Infinity'::numeric)",
            name=op.f("ck_signal_evaluation_price_valid"),
        ),
        sa.CheckConstraint(
            "position_status IS NULL OR position_status IN ('HOLDING','NOT_HOLDING')",
            name=op.f("ck_signal_evaluation_position_status_valid"),
        ),
        sa.CheckConstraint(
            "(low_strong IS NULL AND low_watch IS NULL "
            "AND high_watch IS NULL AND high_strong IS NULL) OR "
            "(low_strong IS NOT NULL AND low_watch IS NOT NULL "
            "AND high_watch IS NOT NULL AND high_strong IS NOT NULL "
            "AND low_strong > 0 AND low_strong <> 'NaN'::numeric "
            "AND high_strong < 'Infinity'::numeric "
            "AND low_strong < low_watch AND low_watch < high_watch "
            "AND high_watch < high_strong)",
            name=op.f("ck_signal_evaluation_target_values_valid"),
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name=op.f("ck_signal_evaluation_content_hash_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["monitor_subscription.id"],
            name=op.f("fk_signal_evaluation_subscription_id_monitor_subscription"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_revision_id"],
            ["target_revision.id"],
            name=op.f("fk_signal_evaluation_target_revision_id_target_revision"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
            name=op.f("fk_signal_evaluation_job_id_job"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signal_evaluation")),
        sa.UniqueConstraint(
            "subscription_id",
            "idempotency_key",
            name="uq_signal_evaluation_idempotency",
        ),
    )
    op.create_index(
        "ix_signal_evaluation_subscription_created",
        "signal_evaluation",
        ["subscription_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "signal_event",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("subscription_id", sa.UUID(), nullable=False),
        sa.Column("evaluation_id", sa.UUID(), nullable=False),
        sa.Column("before_zone", sa.String(16), nullable=False),
        sa.Column("after_zone", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("price", sa.Numeric(20, 6), nullable=False),
        sa.Column("price_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_revision_id", sa.UUID(), nullable=False),
        sa.Column("target_version", sa.Integer(), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("low_strong", sa.Numeric(20, 2), nullable=False),
        sa.Column("low_watch", sa.Numeric(20, 2), nullable=False),
        sa.Column("high_watch", sa.Numeric(20, 2), nullable=False),
        sa.Column("high_strong", sa.Numeric(20, 2), nullable=False),
        sa.Column("position_status", sa.String(16), nullable=False),
        sa.Column("position_version", sa.Integer(), nullable=False),
        sa.Column("quote_cycle_id", sa.UUID(), nullable=True),
        sa.Column("quote_scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quote_item_id", sa.UUID(), nullable=True),
        sa.Column("used_stale_target", sa.Boolean(), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("notification_class", sa.String(16), nullable=False),
        sa.Column("notification_eligible", sa.Boolean(), nullable=False),
        sa.Column("suppression_reason", sa.String(100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"before_zone IN ({ZONES})",
            name=op.f("ck_signal_event_before_zone_valid"),
        ),
        sa.CheckConstraint(
            f"after_zone IN ({ZONES})",
            name=op.f("ck_signal_event_after_zone_valid"),
        ),
        sa.CheckConstraint(
            "before_zone <> after_zone",
            name=op.f("ck_signal_event_real_transition"),
        ),
        sa.CheckConstraint(
            f"reason IN ({REASONS})", name=op.f("ck_signal_event_reason_valid")
        ),
        sa.CheckConstraint(
            "notification_class IN ('LOW','LOW_CLEARED','HIGH','HIGH_CLEARED')",
            name=op.f("ck_signal_event_notification_class_valid"),
        ),
        sa.CheckConstraint(
            "target_version > 0 AND position_version >= 0 AND state_version > 0",
            name=op.f("ck_signal_event_versions_positive"),
        ),
        sa.CheckConstraint(
            "price > 0 AND price <> 'NaN'::numeric AND price < 'Infinity'::numeric",
            name=op.f("ck_signal_event_price_valid"),
        ),
        sa.CheckConstraint(
            "low_strong > 0 AND low_strong <> 'NaN'::numeric "
            "AND high_strong < 'Infinity'::numeric "
            "AND low_strong < low_watch AND low_watch < high_watch "
            "AND high_watch < high_strong",
            name=op.f("ck_signal_event_target_values_valid"),
        ),
        sa.CheckConstraint(
            "position_status IN ('HOLDING','NOT_HOLDING')",
            name=op.f("ck_signal_event_position_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["monitor_subscription.id"],
            name=op.f("fk_signal_event_subscription_id_monitor_subscription"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["signal_evaluation.id"],
            name=op.f("fk_signal_event_evaluation_id_signal_evaluation"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_revision_id"],
            ["target_revision.id"],
            name=op.f("fk_signal_event_target_revision_id_target_revision"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signal_event")),
        sa.UniqueConstraint("evaluation_id", name="uq_signal_event_evaluation"),
    )
    op.create_index(
        "ix_signal_event_subscription_created",
        "signal_event",
        ["subscription_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_signal_event_notification_eligible",
        "signal_event",
        ["notification_eligible"],
        unique=False,
    )

    _protect_immutable_facts()
    role = _application_role()
    op.execute(f"GRANT SELECT, INSERT ON TABLE {', '.join(TABLES)} TO {role}")
    op.execute(f"GRANT UPDATE ON TABLE {', '.join(MUTABLE_TABLES)} TO {role}")
    op.execute(f"REVOKE DELETE ON TABLE {', '.join(TABLES)} FROM {role}")
    op.execute(f"REVOKE UPDATE ON TABLE {', '.join(IMMUTABLE_TABLES)} FROM {role}")


def downgrade() -> None:
    for table_name in reversed(IMMUTABLE_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS {table_name}_append_only ON {table_name}")
    op.execute("DROP FUNCTION IF EXISTS reject_target_signal_fact_mutation()")
    op.drop_index("ix_signal_event_notification_eligible", table_name="signal_event")
    op.drop_index("ix_signal_event_subscription_created", table_name="signal_event")
    op.drop_table("signal_event")
    op.drop_index(
        "ix_signal_evaluation_subscription_created", table_name="signal_evaluation"
    )
    op.drop_table("signal_evaluation")
    op.drop_table("signal_state")
    op.drop_index(
        "ix_subscription_target_binding_status",
        table_name="subscription_target_binding",
    )
    op.drop_table("subscription_target_binding")
    op.drop_index(
        "ix_target_revision_subscription_created", table_name="target_revision"
    )
    op.drop_table("target_revision")
