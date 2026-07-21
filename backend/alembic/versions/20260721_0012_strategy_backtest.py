"""Create strategy and fixed-target holdout backtest storage.

Revision ID: 20260721_0012
Revises: 20260717_0011
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from long_invest.platform.config.settings import get_settings

revision: str = "20260721_0012"
down_revision: str | None = "20260717_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
TABLES = (
    "strategy",
    "strategy_draft",
    "strategy_draft_revision",
    "strategy_validation_run",
    "strategy_version",
    "strategy_run",
    "backtest_task",
    "backtest_universe_snapshot",
    "backtest_item",
    "backtest_forecast_snapshot",
    "backtest_target_adjustment",
    "backtest_order",
    "backtest_trade",
    "backtest_metric",
    "backtest_daily_result",
    "target_calculation_run",
    "target_review",
)
IMMUTABLE_TABLES = (
    "strategy_draft_revision",
    "backtest_universe_snapshot",
    "backtest_forecast_snapshot",
    "backtest_target_adjustment",
    "backtest_trade",
    "backtest_metric",
    "backtest_daily_result",
)
MUTABLE_TABLES = tuple(table for table in TABLES if table not in IMMUTABLE_TABLES)


def _application_role() -> str:
    role = get_settings().database_app_role
    if ROLE_PATTERN.fullmatch(role) is None:
        raise ValueError("unsafe PostgreSQL identifier")
    return f'"{role}"'


def _finite(*columns: str) -> str:
    return " AND ".join(
        f"{column} <> 'NaN'::numeric "
        f"AND {column} < 'Infinity'::numeric "
        f"AND {column} > '-Infinity'::numeric"
        for column in columns
    )


def _ordered_targets(prefix: str = "") -> str:
    low_strong = f"{prefix}low_strong"
    low_watch = f"{prefix}low_watch"
    high_watch = f"{prefix}high_watch"
    high_strong = f"{prefix}high_strong"
    return (
        f"{low_strong} > 0 AND {low_strong} < {low_watch} "
        f"AND {low_watch} < {high_watch} "
        f"AND {high_watch} < {high_strong}"
    )


def _protect_immutable_facts() -> None:
    op.execute(
        """
        CREATE FUNCTION reject_strategy_backtest_fact_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'strategy and backtest historical facts are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table_name in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table_name}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table_name} "
            "FOR EACH ROW EXECUTE FUNCTION "
            "reject_strategy_backtest_fact_mutation()"
        )


def _validate_target_revision_history() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM target_revision
                WHERE content_hash !~ '^[0-9a-f]{64}$'
                   OR (source_code_hash IS NOT NULL
                       AND source_code_hash !~ '^[0-9a-f]{64}$')
            ) THEN
                RAISE EXCEPTION
                    'stage4 migration blocked: target_revision contains '
                    'non-lowercase SHA-256 values';
            END IF;
        END;
        $$
        """
    )


def _protect_published_strategy_versions() -> None:
    op.execute(
        """
        CREATE FUNCTION protect_published_strategy_version()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.status = 'PUBLISHED' AND OLD.status <> 'PUBLISHED' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM strategy_validation_run validation
                    WHERE validation.id = NEW.validation_run_id
                      AND validation.strategy_id = NEW.strategy_id
                      AND validation.strategy_version_id = NEW.id
                      AND validation.source_code_hash = NEW.source_code_hash
                      AND validation.status = 'SUCCEEDED'
                ) THEN
                    RAISE EXCEPTION
                        'published strategy requires matching validation evidence';
                END IF;
            END IF;
            IF OLD.status IN ('PUBLISHED', 'ARCHIVED') THEN
                IF ROW(
                    NEW.id, NEW.strategy_id, NEW.version_no,
                    NEW.source_code_hash, NEW.source_code,
                    NEW.metadata, NEW.parameter_schema,
                    NEW.environment_version, NEW.runner_image_digest,
                    NEW.git_commit, NEW.validation_run_id,
                    NEW.published_at, NEW.created_at
                ) IS DISTINCT FROM ROW(
                    OLD.id, OLD.strategy_id, OLD.version_no,
                    OLD.source_code_hash, OLD.source_code,
                    OLD.metadata, OLD.parameter_schema,
                    OLD.environment_version, OLD.runner_image_digest,
                    OLD.git_commit, OLD.validation_run_id,
                    OLD.published_at, OLD.created_at
                ) THEN
                    RAISE EXCEPTION
                        'published strategy version facts are immutable';
                END IF;
                IF OLD.status = 'ARCHIVED' AND NEW.status <> 'ARCHIVED' THEN
                    RAISE EXCEPTION 'archived strategy version is immutable';
                END IF;
                IF OLD.status = 'PUBLISHED'
                   AND NEW.status NOT IN ('PUBLISHED', 'ARCHIVED') THEN
                    RAISE EXCEPTION
                        'published strategy version can only be archived';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER strategy_version_published_immutable "
        "BEFORE UPDATE ON strategy_version FOR EACH ROW "
        "EXECUTE FUNCTION protect_published_strategy_version()"
    )


def _create_indexes() -> None:
    indexes = (
        ("ix_strategy_status", "strategy", ("status",)),
        (
            "ix_strategy_validation_run_status",
            "strategy_validation_run",
            ("status",),
        ),
        (
            "ix_strategy_validation_run_draft_evidence",
            "strategy_validation_run",
            ("strategy_id", "draft_version", "source_code_hash", "status"),
        ),
        (
            "ix_strategy_run_strategy_version_status",
            "strategy_run",
            ("strategy_version_id", "status"),
        ),
        (
            "ix_backtest_task_status_created",
            "backtest_task",
            ("status", "created_at"),
        ),
        (
            "ix_backtest_task_strategy_version",
            "backtest_task",
            ("strategy_version_id",),
        ),
        (
            "ix_backtest_item_task_status",
            "backtest_item",
            ("task_id", "status"),
        ),
        ("ix_backtest_item_security", "backtest_item", ("security_id",)),
        (
            "ix_backtest_order_item_status",
            "backtest_order",
            ("item_id", "status"),
        ),
        (
            "ix_backtest_trade_item_execute_date",
            "backtest_trade",
            ("item_id", "execute_date"),
        ),
        (
            "ix_target_calculation_run_subscription_created",
            "target_calculation_run",
            ("subscription_id", "created_at"),
        ),
        (
            "ix_target_calculation_run_status",
            "target_calculation_run",
            ("status",),
        ),
        (
            "ix_target_review_status_created",
            "target_review",
            ("status", "created_at"),
        ),
        (
            "ix_target_review_candidate",
            "target_review",
            ("candidate_revision_id",),
        ),
    )
    for index_name, table_name, columns in indexes:
        op.create_index(op.f(index_name), table_name, list(columns), unique=False)


def _create_strategy_tables() -> None:
    op.create_table(
        "strategy",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.CheckConstraint(
            "status IN ('DRAFT','VALIDATING','VALIDATED','PUBLISHING',"
            "'PUBLISHED','PUBLISH_FAILED','ARCHIVED')",
            name=op.f("ck_strategy_status_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategy")),
    )
    op.create_table(
        "strategy_draft",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("strategy_id", sa.UUID(), nullable=False),
        sa.Column("source_code", sa.String(), nullable=False),
        sa.Column("draft_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "draft_version > 0",
            name=op.f("ck_strategy_draft_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id"],
            ["strategy.id"],
            name=op.f("fk_strategy_draft_strategy_id_strategy"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategy_draft")),
        sa.UniqueConstraint("strategy_id", name="uq_strategy_draft_strategy_id"),
    )
    op.create_table(
        "strategy_draft_revision",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("draft_id", sa.UUID(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("source_code", sa.String(), nullable=False),
        sa.CheckConstraint(
            "revision_no > 0",
            name=op.f("ck_strategy_draft_revision_revision_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["draft_id"],
            ["strategy_draft.id"],
            name=op.f("fk_strategy_draft_revision_draft_id_strategy_draft"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategy_draft_revision")),
        sa.UniqueConstraint(
            "draft_id",
            "revision_no",
            name="uq_strategy_draft_revision_draft_id_revision_no",
        ),
    )
    op.create_table(
        "strategy_validation_run",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("strategy_id", sa.UUID(), nullable=False),
        sa.Column("strategy_version_id", sa.UUID(), nullable=True),
        sa.Column("draft_version", sa.Integer(), nullable=False),
        sa.Column("source_code_hash", sa.String(64), nullable=False),
        sa.Column(
            "evidence_snapshot",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED')",
            name=op.f("ck_strategy_validation_run_status_valid"),
        ),
        sa.CheckConstraint(
            "draft_version > 0",
            name=op.f("ck_strategy_validation_run_draft_version_positive"),
        ),
        sa.CheckConstraint(
            "source_code_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_strategy_validation_run_source_code_hash_sha256"),
        ),
        sa.CheckConstraint(
            "(status IN ('PENDING','RUNNING') AND completed_at IS NULL "
            "AND error_code IS NULL) OR "
            "(status = 'SUCCEEDED' AND completed_at IS NOT NULL "
            "AND error_code IS NULL) OR "
            "(status = 'FAILED' AND completed_at IS NOT NULL "
            "AND error_code IS NOT NULL)",
            name=op.f("ck_strategy_validation_run_completion_consistent"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name=op.f("ck_strategy_validation_run_completion_time_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id"],
            ["strategy.id"],
            name=op.f("fk_strategy_validation_run_strategy_id_strategy"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategy_validation_run")),
    )
    op.create_table(
        "strategy_version",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("strategy_id", sa.UUID(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("source_code_hash", sa.String(64), nullable=False),
        sa.Column("source_code", sa.String(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        sa.Column("parameter_schema", postgresql.JSONB(), nullable=False),
        sa.Column("environment_version", sa.String(64), nullable=False),
        sa.Column("runner_image_digest", sa.String(71), nullable=False),
        sa.Column("git_commit", sa.String(64), nullable=True),
        sa.Column("validation_run_id", sa.UUID(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version_no > 0",
            name=op.f("ck_strategy_version_version_positive"),
        ),
        sa.CheckConstraint(
            "source_code_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_strategy_version_source_code_hash_sha256"),
        ),
        sa.CheckConstraint(
            "runner_image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_strategy_version_runner_image_digest_sha256"),
        ),
        sa.CheckConstraint(
            "status IN ('PUBLISHING','PUBLISHED','PUBLISH_FAILED','ARCHIVED')",
            name=op.f("ck_strategy_version_status_valid"),
        ),
        sa.CheckConstraint(
            "(status IN ('PUBLISHED','ARCHIVED') "
            "AND published_at IS NOT NULL AND git_commit IS NOT NULL "
            "AND validation_run_id IS NOT NULL) OR "
            "(status IN ('PUBLISHING','PUBLISH_FAILED') "
            "AND published_at IS NULL)",
            name=op.f("ck_strategy_version_publication_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id"],
            ["strategy.id"],
            name=op.f("fk_strategy_version_strategy_id_strategy"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["validation_run_id"],
            ["strategy_validation_run.id"],
            name=op.f("fk_strategy_version_validation_run_id_strategy_validation_run"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategy_version")),
        sa.UniqueConstraint(
            "strategy_id",
            "version_no",
            name="uq_strategy_version_strategy_id_version_no",
        ),
    )
    op.create_foreign_key(
        op.f("fk_strategy_validation_run_strategy_version_id_strategy_version"),
        "strategy_validation_run",
        "strategy_version",
        ["strategy_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "strategy_run",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("strategy_version_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELED')",
            name=op.f("ck_strategy_run_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"],
            ["strategy_version.id"],
            name=op.f("fk_strategy_run_strategy_version_id_strategy_version"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategy_run")),
    )


def _create_backtest_root_tables() -> None:
    op.create_table(
        "backtest_task",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("universe_hash", sa.String(64), nullable=False),
        sa.Column("training_start_date", sa.Date(), nullable=False),
        sa.Column("training_end_date", sa.Date(), nullable=False),
        sa.Column("test_start_date", sa.Date(), nullable=False),
        sa.Column("test_end_date", sa.Date(), nullable=False),
        sa.Column("strategy_version_id", sa.UUID(), nullable=True),
        sa.Column("draft_source_code", sa.String(), nullable=True),
        sa.Column("source_code_hash", sa.String(64), nullable=False),
        sa.Column(
            "parameter_snapshot",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("parameter_hash", sa.String(64), nullable=False),
        sa.Column("environment_version", sa.String(64), nullable=False),
        sa.Column("runner_image_digest", sa.String(71), nullable=False),
        sa.Column("strategy_api_version", sa.String(32), nullable=False),
        sa.Column("rule_version", sa.String(64), nullable=False),
        sa.Column("hysteresis_ratio", sa.Numeric(10, 6), nullable=False),
        sa.Column("minimum_hysteresis", sa.Numeric(20, 6), nullable=False),
        sa.Column("price_basis", sa.String(32), nullable=False),
        sa.Column("data_source", sa.String(64), nullable=False),
        sa.Column("initial_capital", sa.Numeric(20, 2), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "training_start_date <= training_end_date "
            "AND training_end_date < test_start_date "
            "AND test_start_date <= test_end_date",
            name=op.f("ck_backtest_task_date_range_valid"),
        ),
        sa.CheckConstraint(
            "mode IN ('SINGLE','WATCHLIST','MARKET')",
            name=op.f("ck_backtest_task_mode_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','PAUSING','PAUSED','SUCCEEDED',"
            "'PARTIAL','FAILED','CANCELING','CANCELED')",
            name=op.f("ck_backtest_task_status_valid"),
        ),
        sa.CheckConstraint(
            "(strategy_version_id IS NOT NULL AND draft_source_code IS NULL) "
            "OR (strategy_version_id IS NULL AND draft_source_code IS NOT NULL "
            "AND length(trim(draft_source_code)) > 0)",
            name=op.f("ck_backtest_task_strategy_source_valid"),
        ),
        sa.CheckConstraint(
            "universe_hash ~ '^[0-9a-f]{64}$' "
            "AND source_code_hash ~ '^[0-9a-f]{64}$' "
            "AND parameter_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_backtest_task_hashes_sha256"),
        ),
        sa.CheckConstraint(
            "runner_image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_backtest_task_runner_image_digest_sha256"),
        ),
        sa.CheckConstraint(
            "initial_capital > 0 AND initial_capital <> 'NaN'::numeric "
            "AND initial_capital < 'Infinity'::numeric",
            name=op.f("ck_backtest_task_initial_capital_positive"),
        ),
        sa.CheckConstraint(
            "hysteresis_ratio >= 0 AND minimum_hysteresis >= 0",
            name=op.f("ck_backtest_task_hysteresis_nonnegative"),
        ),
        sa.CheckConstraint(
            _finite("hysteresis_ratio", "minimum_hysteresis", "initial_capital"),
            name=op.f("ck_backtest_task_numeric_finite"),
        ),
        sa.CheckConstraint(
            "length(trim(environment_version)) > 0 "
            "AND length(trim(strategy_api_version)) > 0 "
            "AND length(trim(rule_version)) > 0 "
            "AND length(trim(price_basis)) > 0 "
            "AND length(trim(data_source)) > 0",
            name=op.f("ck_backtest_task_required_text_nonblank"),
        ),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"],
            ["strategy_version.id"],
            name=op.f("fk_backtest_task_strategy_version_id_strategy_version"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_task")),
    )
    op.create_table(
        "backtest_universe_snapshot",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("scope_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_backtest_universe_snapshot_content_hash_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["backtest_task.id"],
            name=op.f("fk_backtest_universe_snapshot_task_id_backtest_task"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_universe_snapshot")),
        sa.UniqueConstraint("task_id", name="uq_backtest_universe_snapshot_task_id"),
    )
    op.create_table(
        "backtest_item",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("failure_code", sa.String(100), nullable=True),
        sa.Column("training_data_fetched_at", sa.DateTime(timezone=True)),
        sa.Column("training_data_start_date", sa.Date()),
        sa.Column("training_data_end_date", sa.Date()),
        sa.Column("training_data_row_count", sa.Integer()),
        sa.Column("training_data_hash", sa.String(64)),
        sa.Column("training_price_basis", sa.String(32)),
        sa.Column("test_data_fetched_at", sa.DateTime(timezone=True)),
        sa.Column("test_data_start_date", sa.Date()),
        sa.Column("test_data_end_date", sa.Date()),
        sa.Column("test_data_row_count", sa.Integer()),
        sa.Column("test_data_hash", sa.String(64)),
        sa.Column("test_price_basis", sa.String(32)),
        sa.CheckConstraint(
            "status IN ('PENDING','FETCHING_DATA','VALIDATING_DATA','FORECASTING',"
            "'FROZEN','SIMULATING','SAVING','SUCCEEDED','FAILED','SKIPPED',"
            "'CANCELED')",
            name=op.f("ck_backtest_item_status_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'FAILED' AND failure_code IS NOT NULL) OR "
            "(status <> 'FAILED' AND failure_code IS NULL)",
            name=op.f("ck_backtest_item_failure_consistent"),
        ),
        sa.CheckConstraint(
            "(training_data_fetched_at IS NULL "
            "AND training_data_start_date IS NULL "
            "AND training_data_end_date IS NULL "
            "AND training_data_row_count IS NULL "
            "AND training_data_hash IS NULL "
            "AND training_price_basis IS NULL) OR "
            "(training_data_fetched_at IS NOT NULL "
            "AND training_data_start_date IS NOT NULL "
            "AND training_data_end_date IS NOT NULL "
            "AND training_data_start_date <= training_data_end_date "
            "AND training_data_row_count > 0 "
            "AND training_data_hash ~ '^[0-9a-f]{64}$' "
            "AND length(trim(training_price_basis)) > 0)",
            name=op.f("ck_backtest_item_training_snapshot_consistent"),
        ),
        sa.CheckConstraint(
            "(test_data_fetched_at IS NULL AND test_data_start_date IS NULL "
            "AND test_data_end_date IS NULL AND test_data_row_count IS NULL "
            "AND test_data_hash IS NULL AND test_price_basis IS NULL) OR "
            "(test_data_fetched_at IS NOT NULL "
            "AND test_data_start_date IS NOT NULL "
            "AND test_data_end_date IS NOT NULL "
            "AND test_data_start_date <= test_data_end_date "
            "AND test_data_row_count > 0 "
            "AND test_data_hash ~ '^[0-9a-f]{64}$' "
            "AND length(trim(test_price_basis)) > 0)",
            name=op.f("ck_backtest_item_test_snapshot_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["backtest_task.id"],
            name=op.f("fk_backtest_item_task_id_backtest_task"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security.id"],
            name=op.f("fk_backtest_item_security_id_security"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_item")),
        sa.UniqueConstraint(
            "task_id",
            "security_id",
            name="uq_backtest_item_task_id_security_id",
        ),
    )


def _create_backtest_snapshot_tables() -> None:
    op.create_table(
        "backtest_forecast_snapshot",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("item_id", sa.UUID(), nullable=False),
        sa.Column("training_start_date", sa.Date(), nullable=False),
        sa.Column("training_end_date", sa.Date(), nullable=False),
        sa.Column("training_row_count", sa.Integer(), nullable=False),
        sa.Column("training_fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("training_data_hash", sa.String(64), nullable=False),
        sa.Column("source_code_hash", sa.String(64), nullable=False),
        sa.Column("parameter_hash", sa.String(64), nullable=False),
        sa.Column("low_strong", sa.Numeric(20, 2), nullable=False),
        sa.Column("low_watch", sa.Numeric(20, 2), nullable=False),
        sa.Column("high_watch", sa.Numeric(20, 2), nullable=False),
        sa.Column("high_strong", sa.Numeric(20, 2), nullable=False),
        sa.Column(
            "diagnostics",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("environment_version", sa.String(64), nullable=False),
        sa.Column("runner_image_digest", sa.String(71), nullable=False),
        sa.Column("price_basis", sa.String(32), nullable=False),
        sa.Column(
            "frozen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "training_start_date <= training_end_date AND training_row_count > 0",
            name=op.f("ck_backtest_forecast_snapshot_training_range_valid"),
        ),
        sa.CheckConstraint(
            "training_fetched_at <= frozen_at",
            name=op.f("ck_backtest_forecast_snapshot_fetch_before_freeze"),
        ),
        sa.CheckConstraint(
            "training_data_hash ~ '^[0-9a-f]{64}$' "
            "AND source_code_hash ~ '^[0-9a-f]{64}$' "
            "AND parameter_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_backtest_forecast_snapshot_hashes_sha256"),
        ),
        sa.CheckConstraint(
            "runner_image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_backtest_forecast_snapshot_runner_image_digest_sha256"),
        ),
        sa.CheckConstraint(
            _ordered_targets(),
            name=op.f("ck_backtest_forecast_snapshot_targets_ordered"),
        ),
        sa.CheckConstraint(
            _finite("low_strong", "low_watch", "high_watch", "high_strong"),
            name=op.f("ck_backtest_forecast_snapshot_numeric_finite"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["backtest_item.id"],
            name=op.f("fk_backtest_forecast_snapshot_item_id_backtest_item"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_forecast_snapshot")),
        sa.UniqueConstraint("item_id", name="uq_backtest_forecast_snapshot_item_id"),
    )
    adjustment_columns = (
        "before_low_strong",
        "before_low_watch",
        "before_high_watch",
        "before_high_strong",
        "after_low_strong",
        "after_low_watch",
        "after_high_watch",
        "after_high_strong",
    )
    op.create_table(
        "backtest_target_adjustment",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("item_id", sa.UUID(), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("adjustment_factor", sa.Numeric(20, 10), nullable=False),
        *(
            sa.Column(column, sa.Numeric(20, 2), nullable=False)
            for column in adjustment_columns
        ),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("data_hash", sa.String(64), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "adjustment_factor > 0 AND adjustment_factor <> 'NaN'::numeric "
            "AND adjustment_factor < 'Infinity'::numeric",
            name=op.f("ck_backtest_target_adjustment_factor_positive"),
        ),
        sa.CheckConstraint(
            "published_at <= effective_at",
            name=op.f("ck_backtest_target_adjustment_publication_before_effective"),
        ),
        sa.CheckConstraint(
            f"{_ordered_targets('before_')} AND {_ordered_targets('after_')}",
            name=op.f("ck_backtest_target_adjustment_targets_ordered"),
        ),
        sa.CheckConstraint(
            "data_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_backtest_target_adjustment_data_hash_sha256"),
        ),
        sa.CheckConstraint(
            _finite("adjustment_factor", *adjustment_columns),
            name=op.f("ck_backtest_target_adjustment_numeric_finite"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["backtest_item.id"],
            name=op.f("fk_backtest_target_adjustment_item_id_backtest_item"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_target_adjustment")),
        sa.UniqueConstraint(
            "item_id",
            "event_date",
            name="uq_backtest_target_adjustment_item_id_event_date",
        ),
    )


def _target_columns() -> tuple[sa.Column, ...]:
    return tuple(
        sa.Column(column, sa.Numeric(20, 2), nullable=False)
        for column in (
            "target_low_strong",
            "target_low_watch",
            "target_high_watch",
            "target_high_strong",
        )
    )


def _create_backtest_result_tables() -> None:
    target_columns = (
        "target_low_strong",
        "target_low_watch",
        "target_high_watch",
        "target_high_strong",
    )
    op.create_table(
        "backtest_order",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("item_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("signal_date", sa.Date(), nullable=False),
        sa.Column("execute_date", sa.Date()),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("execution_price", sa.Numeric(20, 6)),
        sa.Column("quantity", sa.Numeric(20, 6), nullable=False),
        sa.Column("cash_before", sa.Numeric(20, 2), nullable=False),
        sa.Column("position_before", sa.Numeric(20, 6), nullable=False),
        *_target_columns(),
        sa.Column("target_zone", sa.String(16), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING','FILLED','UNFILLED_AT_END')",
            name=op.f("ck_backtest_order_status_valid"),
        ),
        sa.CheckConstraint(
            "direction IN ('BUY','SELL')",
            name=op.f("ck_backtest_order_direction_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'FILLED' AND execute_date IS NOT NULL "
            "AND execute_date > signal_date AND execution_price > 0) OR "
            "(status IN ('PENDING','UNFILLED_AT_END') "
            "AND execute_date IS NULL AND execution_price IS NULL)",
            name=op.f("ck_backtest_order_execution_consistent"),
        ),
        sa.CheckConstraint(
            "quantity > 0 AND cash_before >= 0 AND position_before >= 0",
            name=op.f("ck_backtest_order_quantity_positive"),
        ),
        sa.CheckConstraint(
            _ordered_targets("target_"),
            name=op.f("ck_backtest_order_targets_ordered"),
        ),
        sa.CheckConstraint(
            "target_zone IN ('UNKNOWN','STRONG_LOW','LOW','NORMAL','HIGH',"
            "'STRONG_HIGH')",
            name=op.f("ck_backtest_order_target_zone_valid"),
        ),
        sa.CheckConstraint(
            _finite(
                "execution_price",
                "quantity",
                "cash_before",
                "position_before",
                *target_columns,
            ),
            name=op.f("ck_backtest_order_numeric_finite"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["backtest_item.id"],
            name=op.f("fk_backtest_order_item_id_backtest_item"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_order")),
        sa.UniqueConstraint(
            "item_id",
            "signal_date",
            "direction",
            name="uq_backtest_order_item_id_signal_date_direction",
        ),
    )
    op.create_table(
        "backtest_trade",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("item_id", sa.UUID(), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=False),
        sa.Column("execute_date", sa.Date(), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("price", sa.Numeric(20, 6), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 6), nullable=False),
        sa.Column("cash_after", sa.Numeric(20, 2), nullable=False),
        sa.Column("position_after", sa.Numeric(20, 6), nullable=False),
        *_target_columns(),
        sa.Column("target_zone", sa.String(16), nullable=False),
        sa.Column("round_trip_no", sa.Integer(), nullable=False),
        sa.Column("holding_trade_days", sa.Integer()),
        sa.Column("realized_return_amount", sa.Numeric(20, 2)),
        sa.Column("realized_return_rate", sa.Numeric(20, 8)),
        sa.CheckConstraint(
            "direction IN ('BUY','SELL')",
            name=op.f("ck_backtest_trade_direction_valid"),
        ),
        sa.CheckConstraint(
            "price > 0 AND quantity > 0 AND cash_after >= 0 "
            "AND position_after >= 0 AND round_trip_no > 0 "
            "AND (holding_trade_days IS NULL OR holding_trade_days >= 0) "
            "AND ((direction = 'SELL' AND holding_trade_days IS NOT NULL "
            "AND realized_return_amount IS NOT NULL "
            "AND realized_return_rate IS NOT NULL) OR "
            "(direction = 'BUY' AND holding_trade_days IS NULL "
            "AND realized_return_amount IS NULL "
            "AND realized_return_rate IS NULL))",
            name=op.f("ck_backtest_trade_values_valid"),
        ),
        sa.CheckConstraint(
            _ordered_targets("target_"),
            name=op.f("ck_backtest_trade_targets_ordered"),
        ),
        sa.CheckConstraint(
            "target_zone IN ('UNKNOWN','STRONG_LOW','LOW','NORMAL','HIGH',"
            "'STRONG_HIGH')",
            name=op.f("ck_backtest_trade_target_zone_valid"),
        ),
        sa.CheckConstraint(
            _finite(
                "price",
                "quantity",
                "cash_after",
                "position_after",
                *target_columns,
                "realized_return_amount",
                "realized_return_rate",
            ),
            name=op.f("ck_backtest_trade_numeric_finite"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["backtest_item.id"],
            name=op.f("fk_backtest_trade_item_id_backtest_item"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["backtest_order.id"],
            name=op.f("fk_backtest_trade_order_id_backtest_order"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_trade")),
        sa.UniqueConstraint("order_id", name="uq_backtest_trade_order_id"),
    )
    required_metric_columns = (
        "ending_equity",
        "total_return",
        "realized_return",
        "annualized_return",
        "max_drawdown",
        "volatility",
        "capital_exposure_ratio",
    )
    optional_metric_columns = (
        "sharpe_ratio",
        "win_rate",
        "average_trade_return",
        "maximum_trade_gain",
        "maximum_trade_loss",
        "average_holding_trade_days",
    )
    metric_columns = required_metric_columns + optional_metric_columns
    op.create_table(
        "backtest_metric",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("item_id", sa.UUID(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("ending_equity", sa.Numeric(20, 2), nullable=False),
        *(
            sa.Column(column, sa.Numeric(20, 8), nullable=False)
            for column in required_metric_columns[1:]
        ),
        *(
            sa.Column(column, sa.Numeric(20, 8), nullable=True)
            for column in optional_metric_columns
        ),
        sa.Column("completed_round_trips", sa.Integer(), nullable=False),
        sa.Column("winning_trades", sa.Integer(), nullable=False),
        sa.Column("losing_trades", sa.Integer(), nullable=False),
        sa.Column("breakeven_trades", sa.Integer(), nullable=False),
        sa.Column("longest_holding_trade_days", sa.Integer(), nullable=False),
        sa.Column("open_position_at_end", sa.Boolean(), nullable=False),
        sa.Column("unfilled_order_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_backtest_metric_content_hash_sha256"),
        ),
        sa.CheckConstraint(
            "completed_round_trips >= 0 AND winning_trades >= 0 "
            "AND losing_trades >= 0 AND breakeven_trades >= 0 "
            "AND winning_trades + losing_trades + breakeven_trades "
            "= completed_round_trips "
            "AND longest_holding_trade_days >= 0 "
            "AND unfilled_order_count >= 0",
            name=op.f("ck_backtest_metric_counts_nonnegative"),
        ),
        sa.CheckConstraint(
            "ending_equity >= 0 AND max_drawdown >= 0 AND max_drawdown <= 1 "
            "AND volatility >= 0 AND capital_exposure_ratio >= 0 "
            "AND capital_exposure_ratio <= 1",
            name=op.f("ck_backtest_metric_values_valid"),
        ),
        sa.CheckConstraint(
            "(completed_round_trips = 0 AND win_rate IS NULL) OR "
            "(completed_round_trips > 0 AND win_rate >= 0 AND win_rate <= 1)",
            name=op.f("ck_backtest_metric_win_rate_consistent"),
        ),
        sa.CheckConstraint(
            _finite(*metric_columns),
            name=op.f("ck_backtest_metric_numeric_finite"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["backtest_item.id"],
            name=op.f("fk_backtest_metric_item_id_backtest_item"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_metric")),
        sa.UniqueConstraint("item_id", name="uq_backtest_metric_item_id"),
    )
    daily_numeric_columns = (
        "cash",
        "position_quantity",
        "close_price",
        "position_market_value",
        "equity",
        "drawdown",
        *target_columns,
    )
    op.create_table(
        "backtest_daily_result",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("item_id", sa.UUID(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("cash", sa.Numeric(20, 2), nullable=False),
        sa.Column("position_quantity", sa.Numeric(20, 6), nullable=False),
        sa.Column("close_price", sa.Numeric(20, 6), nullable=False),
        sa.Column("position_market_value", sa.Numeric(20, 2), nullable=False),
        sa.Column("equity", sa.Numeric(20, 2), nullable=False),
        sa.Column("drawdown", sa.Numeric(20, 8), nullable=False),
        *_target_columns(),
        sa.Column("zone", sa.String(16), nullable=False),
        sa.Column("position_status", sa.String(16), nullable=False),
        sa.CheckConstraint(
            "cash >= 0 AND position_quantity >= 0 AND close_price > 0 "
            "AND position_market_value >= 0 AND equity >= 0 "
            "AND equity = cash + position_market_value "
            "AND drawdown >= 0 AND drawdown <= 1",
            name=op.f("ck_backtest_daily_result_values_valid"),
        ),
        sa.CheckConstraint(
            _ordered_targets("target_"),
            name=op.f("ck_backtest_daily_result_targets_ordered"),
        ),
        sa.CheckConstraint(
            "position_status IN ('FLAT','HOLDING')",
            name=op.f("ck_backtest_daily_result_position_status_valid"),
        ),
        sa.CheckConstraint(
            "zone IN ('UNKNOWN','STRONG_LOW','LOW','NORMAL','HIGH','STRONG_HIGH')",
            name=op.f("ck_backtest_daily_result_zone_valid"),
        ),
        sa.CheckConstraint(
            _finite(*daily_numeric_columns),
            name=op.f("ck_backtest_daily_result_numeric_finite"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["backtest_item.id"],
            name=op.f("fk_backtest_daily_result_item_id_backtest_item"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_daily_result")),
        sa.UniqueConstraint(
            "item_id",
            "trade_date",
            name="uq_backtest_daily_result_item_id_trade_date",
        ),
    )


def _create_target_workflow_tables() -> None:
    op.create_table(
        "target_calculation_run",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("subscription_id", sa.UUID(), nullable=False),
        sa.Column("strategy_version_id", sa.UUID(), nullable=False),
        sa.Column(
            "parameter_snapshot",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("failure_code", sa.String(100)),
        sa.Column("training_start_date", sa.Date()),
        sa.Column("training_end_date", sa.Date()),
        sa.Column("qfq_data_version", sa.Integer()),
        sa.Column("current_target_version", sa.Integer()),
        sa.Column("reason", sa.String(500)),
        sa.Column(
            "resource_usage",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_summary", sa.String(500)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED')",
            name=op.f("ck_target_calculation_run_status_valid"),
        ),
        sa.CheckConstraint(
            "(training_start_date IS NULL AND training_end_date IS NULL) OR "
            "(training_start_date IS NOT NULL AND training_end_date IS NOT NULL "
            "AND training_start_date <= training_end_date)",
            name=op.f("ck_target_calculation_run_training_range_valid"),
        ),
        sa.CheckConstraint(
            "(qfq_data_version IS NULL OR qfq_data_version > 0) "
            "AND (current_target_version IS NULL OR current_target_version > 0)",
            name=op.f("ck_target_calculation_run_versions_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["monitor_subscription.id"],
            name=op.f("fk_target_calculation_run_subscription_id_monitor_subscription"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"],
            ["strategy_version.id"],
            name=op.f("fk_target_calculation_run_strategy_version_id_strategy_version"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_target_calculation_run")),
    )
    op.create_table(
        "target_review",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("candidate_revision_id", sa.UUID(), nullable=False),
        sa.Column("baseline_revision_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("low_strong_change", sa.Numeric(20, 6), nullable=False),
        sa.Column("low_watch_change", sa.Numeric(20, 6), nullable=False),
        sa.Column("high_watch_change", sa.Numeric(20, 6), nullable=False),
        sa.Column("high_strong_change", sa.Numeric(20, 6), nullable=False),
        sa.Column("reviewer_user_id", sa.String(64)),
        sa.Column("review_comment", sa.String(500)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','APPROVED','REJECTED','SUPERSEDED')",
            name=op.f("ck_target_review_status_valid"),
        ),
        sa.CheckConstraint(
            "((status IN ('APPROVED','REJECTED')) "
            "AND reviewer_user_id IS NOT NULL "
            "AND length(trim(reviewer_user_id)) > 0 "
            "AND review_comment IS NOT NULL "
            "AND length(trim(review_comment)) > 0 "
            "AND reviewed_at IS NOT NULL) OR "
            "((status IN ('PENDING','SUPERSEDED')) "
            "AND reviewer_user_id IS NULL AND review_comment IS NULL "
            "AND reviewed_at IS NULL)",
            name=op.f("ck_target_review_decision_consistent"),
        ),
        sa.CheckConstraint(
            "baseline_revision_id IS NULL OR "
            "baseline_revision_id <> candidate_revision_id",
            name=op.f("ck_target_review_distinct_revisions"),
        ),
        sa.ForeignKeyConstraint(
            ["candidate_revision_id"],
            ["target_revision.id"],
            name=op.f("fk_target_review_candidate_revision_id_target_revision"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["baseline_revision_id"],
            ["target_revision.id"],
            name=op.f("fk_target_review_baseline_revision_id_target_revision"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_target_review")),
    )


def upgrade() -> None:
    _validate_target_revision_history()
    _create_strategy_tables()
    _create_backtest_root_tables()
    _create_backtest_snapshot_tables()
    _create_backtest_result_tables()
    _create_target_workflow_tables()
    _create_indexes()

    op.drop_constraint(
        op.f("ck_target_revision_source_valid"),
        "target_revision",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_target_revision_source_revision_consistent"),
        "target_revision",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_target_revision_content_hash_sha256"),
        "target_revision",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_target_revision_source_code_hash_sha256"),
        "target_revision",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_target_revision_source_valid"),
        "target_revision",
        "source IN ('MANUAL','STRATEGY','RESTORED','DATA_CORRECTION',"
        "'STRATEGY_CHANGE','PARAMETER_CHANGE')",
    )
    op.create_check_constraint(
        op.f("ck_target_revision_source_revision_consistent"),
        "target_revision",
        "(source = 'RESTORED' AND source_revision_id IS NOT NULL) OR "
        "(source <> 'RESTORED' AND source_revision_id IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_target_revision_content_hash_sha256"),
        "target_revision",
        "content_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        op.f("ck_target_revision_source_code_hash_sha256"),
        "target_revision",
        "source_code_hash IS NULL OR source_code_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        op.f("ck_target_revision_strategy_version_consistent"),
        "target_revision",
        "(source = 'STRATEGY' AND strategy_version_id IS NOT NULL) OR "
        "(source <> 'STRATEGY' AND strategy_version_id IS NULL)",
    )
    op.create_foreign_key(
        op.f("fk_target_revision_strategy_version_id_strategy_version"),
        "target_revision",
        "strategy_version",
        ["strategy_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    _protect_immutable_facts()
    _protect_published_strategy_versions()
    role = _application_role()
    op.execute(f"GRANT SELECT, INSERT ON TABLE {', '.join(TABLES)} TO {role}")
    op.execute(f"GRANT UPDATE ON TABLE {', '.join(MUTABLE_TABLES)} TO {role}")
    op.execute(f"REVOKE UPDATE ON TABLE {', '.join(IMMUTABLE_TABLES)} FROM {role}")
    op.execute(f"REVOKE DELETE ON TABLE {', '.join(TABLES)} FROM {role}")


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM target_revision
                WHERE source NOT IN ('MANUAL', 'RESTORED')
                   OR (source = 'RESTORED' AND source_revision_id IS NULL)
                   OR (source = 'MANUAL' AND source_revision_id IS NOT NULL)
            ) THEN
                RAISE EXCEPTION
                    'stage4 downgrade blocked: target_revision contains '
                    'sources unsupported by revision 0011';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS strategy_version_published_immutable "
        "ON strategy_version"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_published_strategy_version")
    for table_name in reversed(IMMUTABLE_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS {table_name}_append_only ON {table_name}")
    op.execute("DROP FUNCTION IF EXISTS reject_strategy_backtest_fact_mutation")

    op.drop_constraint(
        op.f("fk_target_revision_strategy_version_id_strategy_version"),
        "target_revision",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("ck_target_revision_strategy_version_consistent"),
        "target_revision",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_target_revision_source_revision_consistent"),
        "target_revision",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_target_revision_source_valid"),
        "target_revision",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_target_revision_source_valid"),
        "target_revision",
        "source IN ('MANUAL','RESTORED')",
    )
    op.create_check_constraint(
        op.f("ck_target_revision_source_revision_consistent"),
        "target_revision",
        "(source = 'RESTORED' AND source_revision_id IS NOT NULL) OR "
        "(source = 'MANUAL' AND source_revision_id IS NULL)",
    )
    op.drop_constraint(
        op.f("ck_target_revision_source_code_hash_sha256"),
        "target_revision",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_target_revision_content_hash_sha256"),
        "target_revision",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_target_revision_content_hash_sha256"),
        "target_revision",
        "length(content_hash) = 64",
    )
    op.create_check_constraint(
        op.f("ck_target_revision_source_code_hash_sha256"),
        "target_revision",
        "source_code_hash IS NULL OR length(source_code_hash) = 64",
    )

    op.drop_constraint(
        op.f("fk_strategy_validation_run_strategy_version_id_strategy_version"),
        "strategy_validation_run",
        type_="foreignkey",
    )
    for table_name in reversed(TABLES):
        op.drop_table(table_name)
