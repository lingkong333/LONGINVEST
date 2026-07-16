"""Create market data collection storage.

Revision ID: 20260715_0008
Revises: 20260715_0007
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260715_0008"
down_revision: str | None = "20260715_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PARTITION_YEARS = (2025, 2026, 2027)


def upgrade() -> None:
    op.execute(
        "DROP TRIGGER security_universe_snapshot_item_append_only "
        "ON security_universe_snapshot_item"
    )
    op.add_column(
        "security_universe_snapshot_item",
        sa.Column("security_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "security_universe_snapshot_item",
        sa.Column("listed_on", sa.Date(), nullable=True),
    )
    op.add_column(
        "security_universe_snapshot_item",
        sa.Column("delisted_on", sa.Date(), nullable=True),
    )
    op.execute(
        """
        UPDATE security_universe_snapshot_item AS item
        SET security_id = security.id,
            listed_on = security.listed_on,
            delisted_on = security.delisted_on
        FROM security
        WHERE security.symbol = item.symbol
        """
    )
    op.alter_column(
        "security_universe_snapshot_item", "security_id", nullable=False
    )
    op.create_foreign_key(
        "fk_security_universe_snapshot_item_security_id_security",
        "security_universe_snapshot_item",
        "security",
        ["security_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        "CREATE TRIGGER security_universe_snapshot_item_append_only "
        "BEFORE UPDATE OR DELETE ON security_universe_snapshot_item "
        "FOR EACH ROW EXECUTE FUNCTION reject_stage2_fact_mutation()"
    )
    op.add_column(
        "job",
        sa.Column(
            "soft_timeout_seconds", sa.Integer(), server_default="300", nullable=False
        ),
    )
    op.add_column(
        "job",
        sa.Column(
            "hard_timeout_seconds", sa.Integer(), server_default="360", nullable=False
        ),
    )
    op.create_check_constraint(
        op.f("ck_job_soft_timeout_positive"), "job", "soft_timeout_seconds > 0"
    )
    op.create_check_constraint(
        op.f("ck_job_hard_timeout_not_less_than_soft"),
        "job",
        "hard_timeout_seconds >= soft_timeout_seconds AND hard_timeout_seconds <= 3600",
    )

    op.execute(
        """
        CREATE TABLE data_quality_issue (
            id UUID NOT NULL,
            issue_type VARCHAR(100) NOT NULL,
            subject_type VARCHAR(64) NOT NULL,
            subject_id VARCHAR(128) NOT NULL,
            symbol VARCHAR(16),
            status VARCHAR(32) NOT NULL,
            severity VARCHAR(16) NOT NULL,
            evidence JSONB NOT NULL,
            dedupe_key VARCHAR(200) NOT NULL,
            occurrence_count INTEGER DEFAULT 1 NOT NULL,
            first_seen_at TIMESTAMPTZ DEFAULT now() NOT NULL,
            last_seen_at TIMESTAMPTZ DEFAULT now() NOT NULL,
            resolved_at TIMESTAMPTZ,
            resolved_by_user_id VARCHAR(64),
            resolution_action VARCHAR(32),
            resolution_reason VARCHAR(500),
            selected_source VARCHAR(64),
            CONSTRAINT pk_data_quality_issue PRIMARY KEY (id),
            CONSTRAINT uq_data_quality_issue_dedupe_key UNIQUE (dedupe_key),
            CONSTRAINT ck_data_quality_issue_occurrence_count_positive
                CHECK (occurrence_count > 0),
            CONSTRAINT ck_data_quality_issue_status_valid
                CHECK (status IN ('OPEN','REVIEW_REQUIRED','RESOLVED','INVALIDATED')),
            CONSTRAINT ck_data_quality_issue_severity_valid
                CHECK (severity IN ('INFO','WARNING','ERROR','CRITICAL')),
            CONSTRAINT ck_data_quality_issue_evidence_non_empty_object
                CHECK (jsonb_typeof(evidence) = 'object' AND evidence <> '{}'::jsonb)
        )
        """
    )
    op.create_index(
        "ix_data_quality_issue_status_last_seen",
        "data_quality_issue",
        ["status", "last_seen_at"],
    )
    op.create_index(
        "ix_data_quality_issue_symbol_status",
        "data_quality_issue",
        ["symbol", "status"],
    )

    op.execute(
        """
        CREATE TABLE quote_cycle (
            id UUID NOT NULL,
            status VARCHAR(16) NOT NULL,
            scheduled_at TIMESTAMPTZ NOT NULL,
            schedule_occurrence_id UUID,
            started_at TIMESTAMPTZ,
            deadline_at TIMESTAMPTZ,
            finalized_at TIMESTAMPTZ,
            universe_snapshot_id VARCHAR(200) NOT NULL,
            universe_snapshot_version INTEGER NOT NULL,
            subscription_snapshot_version INTEGER,
            idempotency_scope VARCHAR(200) NOT NULL,
            idempotency_key VARCHAR(200) NOT NULL,
            expected_count INTEGER NOT NULL,
            timeout_seconds INTEGER NOT NULL,
            valid_count INTEGER NOT NULL,
            missing_count INTEGER NOT NULL,
            conflict_count INTEGER NOT NULL,
            failed_count INTEGER NOT NULL,
            cancel_reason VARCHAR(500),
            CONSTRAINT pk_quote_cycle PRIMARY KEY (id),
            CONSTRAINT uq_quote_cycle_idempotency
                UNIQUE (idempotency_scope, idempotency_key),
            CONSTRAINT uq_quote_cycle_schedule_occurrence
                UNIQUE (schedule_occurrence_id),
            CONSTRAINT ck_quote_cycle_deadline CHECK (deadline_at > started_at),
            CONSTRAINT ck_quote_cycle_expected_positive CHECK (expected_count > 0),
            CONSTRAINT ck_quote_cycle_timeout_supported
                CHECK (timeout_seconds BETWEEN 10 AND 60),
            CONSTRAINT ck_quote_cycle_subscription_snapshot_positive
                CHECK (subscription_snapshot_version IS NULL
                    OR subscription_snapshot_version > 0),
            CONSTRAINT ck_quote_cycle_counts_nonnegative
                CHECK (valid_count >= 0 AND missing_count >= 0
                    AND conflict_count >= 0 AND failed_count >= 0),
            CONSTRAINT ck_quote_cycle_status_valid
                CHECK (status IN ('PENDING','FETCHING','FINALIZING','READY',
                    'PARTIAL','FAILED','MISSED','CANCELED'))
        )
        """
    )
    op.create_index(
        "ix_quote_cycle_status_deadline",
        "quote_cycle",
        ["status", "deadline_at"],
    )
    op.execute(
        """
        CREATE TABLE quote_cycle_item (
            id UUID NOT NULL,
            cycle_id UUID NOT NULL,
            symbol VARCHAR(16) NOT NULL,
            expected_subscription_version INTEGER,
            status VARCHAR(32) NOT NULL,
            price NUMERIC(20, 6),
            open NUMERIC(20, 6),
            high NUMERIC(20, 6),
            low NUMERIC(20, 6),
            previous_close NUMERIC(20, 6),
            volume INTEGER,
            amount NUMERIC(24, 4),
            quote_time TIMESTAMPTZ,
            received_at TIMESTAMPTZ,
            provider VARCHAR(32),
            error_code VARCHAR(80),
            conflict_evidence JSONB,
            eligible_for_evaluation BOOLEAN NOT NULL,
            CONSTRAINT pk_quote_cycle_item PRIMARY KEY (id),
            CONSTRAINT uq_quote_cycle_item_symbol UNIQUE (cycle_id, symbol),
            CONSTRAINT ck_quote_cycle_item_volume_nonnegative
                CHECK (volume IS NULL OR volume >= 0),
            CONSTRAINT ck_quote_cycle_item_amount_nonnegative
                CHECK (amount IS NULL OR amount >= 0),
            CONSTRAINT ck_quote_cycle_item_expected_subscription_positive
                CHECK (expected_subscription_version IS NULL
                    OR expected_subscription_version > 0),
            CONSTRAINT ck_quote_cycle_item_status_valid
                CHECK (status IN ('VALID','MISSING','STALE','CONFLICT','INVALID',
                    'TIMEOUT','PROVIDER_FAILED','NOT_EXPECTED_TO_TRADE')),
            CONSTRAINT fk_quote_cycle_item_cycle_id_quote_cycle
                FOREIGN KEY(cycle_id) REFERENCES quote_cycle (id) ON DELETE CASCADE
        )
        """
    )
    op.create_index(
        "ix_quote_cycle_item_cycle_status",
        "quote_cycle_item",
        ["cycle_id", "status"],
    )

    op.execute(
        """
        CREATE TABLE daily_data_batch (
            id UUID NOT NULL,
            trading_date DATE NOT NULL,
            universe_snapshot_id UUID NOT NULL,
            parent_batch_id UUID,
            symbols JSONB NOT NULL,
            security_ids JSONB NOT NULL,
            known_corporate_action_symbols JSONB NOT NULL,
            idempotency_key VARCHAR(160) NOT NULL,
            status VARCHAR(24) NOT NULL,
            expected_count INTEGER NOT NULL,
            fetched_count INTEGER NOT NULL,
            validated_count INTEGER NOT NULL,
            committed_count INTEGER NOT NULL,
            missing_count INTEGER NOT NULL,
            failed_count INTEGER NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
            started_at TIMESTAMPTZ,
            deadline_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            CONSTRAINT pk_daily_data_batch PRIMARY KEY (id),
            CONSTRAINT uq_daily_batch_idempotency UNIQUE (idempotency_key),
            CONSTRAINT ck_daily_data_batch_daily_batch_expected_positive
                CHECK (expected_count > 0),
            CONSTRAINT ck_daily_data_batch_daily_batch_status_valid
                CHECK (status IN ('PENDING','FETCHING','VALIDATING','COMMITTING',
                    'SUCCEEDED','PARTIAL','FAILED')),
            CONSTRAINT fk_daily_data_batch_parent_batch_id_daily_data_batch
                FOREIGN KEY(parent_batch_id) REFERENCES daily_data_batch (id)
                ON DELETE RESTRICT
        )
        """
    )
    op.create_index(
        "ix_daily_batch_date_status",
        "daily_data_batch",
        ["trading_date", "status"],
    )
    op.create_index(
        "uq_daily_batch_auto_scope",
        "daily_data_batch",
        ["trading_date", "universe_snapshot_id"],
        unique=True,
        postgresql_where=sa.text("parent_batch_id IS NULL"),
    )
    op.execute(
        """
        CREATE TABLE daily_bar_stage (
            id UUID NOT NULL,
            batch_id UUID NOT NULL,
            security_id UUID NOT NULL,
            symbol VARCHAR(16) NOT NULL,
            trading_date DATE NOT NULL,
            status VARCHAR(24) NOT NULL,
            provider_payload JSONB,
            missing_reason VARCHAR(32),
            error_code VARCHAR(100),
            quality_code VARCHAR(100),
            received_at TIMESTAMPTZ NOT NULL,
            validated_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT pk_daily_bar_stage PRIMARY KEY (id),
            CONSTRAINT uq_daily_stage_symbol UNIQUE (batch_id, symbol),
            CONSTRAINT ck_daily_bar_stage_daily_stage_status_valid
                CHECK (status IN ('FETCHED','VALID','REVIEW_REQUIRED',
                    'INVALID','MISSING','FAILED')),
            CONSTRAINT fk_daily_bar_stage_batch_id_daily_data_batch
                FOREIGN KEY(batch_id) REFERENCES daily_data_batch (id) ON DELETE CASCADE
        )
        """
    )
    op.create_index(
        "ix_daily_stage_batch_status",
        "daily_bar_stage",
        ["batch_id", "status"],
    )
    op.create_index(
        "ix_daily_stage_expires_at", "daily_bar_stage", ["expires_at"]
    )

    op.execute(
        """
        CREATE TABLE daily_bar_unadjusted (
            security_id UUID NOT NULL,
            trade_date DATE NOT NULL,
            symbol VARCHAR(16) NOT NULL,
            open NUMERIC(18, 6) NOT NULL,
            high NUMERIC(18, 6) NOT NULL,
            low NUMERIC(18, 6) NOT NULL,
            close NUMERIC(18, 6) NOT NULL,
            previous_close NUMERIC(18, 6),
            volume BIGINT NOT NULL,
            amount NUMERIC(24, 4) NOT NULL,
            source VARCHAR(32) NOT NULL,
            data_version INTEGER NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT now() NOT NULL,
            CONSTRAINT pk_daily_bar_unadjusted PRIMARY KEY (security_id, trade_date),
            CONSTRAINT ck_daily_bar_unadjusted_daily_bar_prices_positive
                CHECK (open > 0 AND high > 0 AND low > 0 AND close > 0),
            CONSTRAINT ck_daily_bar_unadjusted_daily_bar_ohlc_valid
                CHECK (high >= open AND high >= close AND high >= low
                    AND low <= open AND low <= close AND low <= high),
            CONSTRAINT ck_daily_bar_unadjusted_daily_bar_quantities_nonnegative
                CHECK (volume >= 0 AND amount >= 0)
        ) PARTITION BY RANGE (trade_date)
        """
    )
    for year in PARTITION_YEARS:
        op.execute(
            f"""
            CREATE TABLE daily_bar_unadjusted_{year}
            PARTITION OF daily_bar_unadjusted
            FOR VALUES FROM ('{year}-01-01') TO ('{year + 1}-01-01')
            """
        )
    op.create_index(
        "ix_daily_bar_symbol_date",
        "daily_bar_unadjusted",
        ["symbol", "trade_date"],
    )
    op.execute(
        """
        CREATE TABLE daily_bar_revision (
            id UUID NOT NULL,
            daily_bar_security_id UUID NOT NULL,
            daily_bar_trade_date DATE NOT NULL,
            symbol VARCHAR(16) NOT NULL,
            revision_no INTEGER NOT NULL,
            old_values JSONB NOT NULL,
            new_values JSONB NOT NULL,
            changed_fields JSONB NOT NULL,
            source VARCHAR(32) NOT NULL,
            reason VARCHAR(500) NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
            CONSTRAINT pk_daily_bar_revision PRIMARY KEY (id),
            CONSTRAINT fk_daily_revision_bar
                FOREIGN KEY(daily_bar_security_id, daily_bar_trade_date)
                REFERENCES daily_bar_unadjusted (security_id, trade_date)
                ON DELETE RESTRICT,
            CONSTRAINT uq_daily_bar_revision_no
                UNIQUE (daily_bar_security_id, daily_bar_trade_date, revision_no)
        )
        """
    )
    op.create_index(
        "ix_daily_revision_symbol_date",
        "daily_bar_revision",
        ["symbol", "daily_bar_trade_date"],
    )
    op.execute(
        """
        CREATE TABLE daily_batch_missing_item (
            id UUID NOT NULL,
            batch_id UUID NOT NULL,
            security_id UUID,
            symbol VARCHAR(16) NOT NULL,
            reason VARCHAR(32) NOT NULL,
            error_code VARCHAR(100),
            explained BOOLEAN NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
            CONSTRAINT pk_daily_batch_missing_item PRIMARY KEY (id),
            CONSTRAINT uq_daily_missing_symbol UNIQUE (batch_id, symbol),
            CONSTRAINT ck_daily_batch_missing_item_daily_missing_reason_valid
                CHECK (reason IN ('SUSPENDED','NOT_YET_LISTED','DELISTED',
                    'NOT_EXPECTED_TO_TRADE','UNEXPLAINED')),
            CONSTRAINT fk_daily_batch_missing_item_batch_id_daily_data_batch
                FOREIGN KEY(batch_id) REFERENCES daily_data_batch (id) ON DELETE CASCADE
        )
        """
    )
    op.create_index(
        "ix_daily_missing_batch_explained",
        "daily_batch_missing_item",
        ["batch_id", "explained"],
    )


def downgrade() -> None:
    op.drop_table("daily_batch_missing_item")
    op.drop_table("daily_bar_revision")
    op.drop_index("ix_daily_bar_symbol_date", table_name="daily_bar_unadjusted")
    op.drop_table("daily_bar_unadjusted")
    op.drop_table("daily_bar_stage")
    op.drop_table("daily_data_batch")
    op.drop_table("quote_cycle_item")
    op.drop_table("quote_cycle")
    op.drop_table("data_quality_issue")
    op.drop_constraint(
        "ck_job_hard_timeout_not_less_than_soft", "job", type_="check"
    )
    op.drop_constraint("ck_job_soft_timeout_positive", "job", type_="check")
    op.drop_column("job", "hard_timeout_seconds")
    op.drop_column("job", "soft_timeout_seconds")
    op.drop_constraint(
        "fk_security_universe_snapshot_item_security_id_security",
        "security_universe_snapshot_item",
        type_="foreignkey",
    )
    op.drop_column("security_universe_snapshot_item", "security_id")
    op.drop_column("security_universe_snapshot_item", "delisted_on")
    op.drop_column("security_universe_snapshot_item", "listed_on")
