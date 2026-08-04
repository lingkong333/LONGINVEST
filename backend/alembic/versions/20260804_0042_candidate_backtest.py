"""Connect candidate screenings to multi-period backtests and price history.

Revision ID: 20260804_0042
Revises: 20260804_0041
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260804_0042"
down_revision: str | None = "20260804_0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column in ("buy_count", "sell_count"):
        op.add_column(
            "backtest_metric",
            sa.Column(
                column, sa.Integer(), server_default="0", nullable=False
            ),
        )
    for column in (
        "gross_profit_amount",
        "gross_loss_amount",
        "net_profit_amount",
    ):
        op.add_column(
            "backtest_metric",
            sa.Column(
                column,
                sa.Numeric(20, 2),
                server_default="0",
                nullable=False,
            ),
        )
    op.add_column(
        "backtest_metric",
        sa.Column("profit_factor", sa.Numeric(20, 8), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_backtest_metric_v4_counts_nonnegative"),
        "backtest_metric",
        "buy_count >= 0 AND sell_count >= 0",
    )
    op.create_check_constraint(
        op.f("ck_backtest_metric_v4_amounts_valid"),
        "backtest_metric",
        "gross_profit_amount >= 0 AND gross_loss_amount >= 0 "
        "AND net_profit_amount = gross_profit_amount - gross_loss_amount "
        "AND (profit_factor IS NULL OR profit_factor >= 0)",
    )
    op.add_column(
        "backtest_task", sa.Column("screening_batch_id", sa.UUID(), nullable=True)
    )
    op.add_column("backtest_task", sa.Column("job_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_backtest_task_screening_batch_id_strategy_screening_batch"),
        "backtest_task",
        "strategy_screening_batch",
        ["screening_batch_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        op.f("ix_backtest_task_screening_batch"),
        "backtest_task",
        ["screening_batch_id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_backtest_task_job_id_job"),
        "backtest_task",
        "job",
        ["job_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        op.f("uq_backtest_task_job_id"), "backtest_task", ["job_id"]
    )

    op.drop_constraint(
        op.f("uq_backtest_item_task_id_security_id"),
        "backtest_item",
        type_="unique",
    )
    op.add_column(
        "backtest_item", sa.Column("screening_result_id", sa.UUID(), nullable=True)
    )
    op.add_column(
        "backtest_item", sa.Column("screening_period_id", sa.UUID(), nullable=True)
    )
    op.add_column(
        "backtest_item", sa.Column("recompute_from_date", sa.Date(), nullable=True)
    )
    op.add_column(
        "backtest_item",
        sa.Column(
            "price_version", sa.Integer(), server_default="1", nullable=False
        ),
    )
    op.create_foreign_key(
        op.f("fk_backtest_item_screening_result_id_strategy_screening_result"),
        "backtest_item",
        "strategy_screening_result",
        ["screening_result_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_backtest_item_screening_period_id_strategy_screening_period"),
        "backtest_item",
        "strategy_screening_period",
        ["screening_period_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        op.f("uq_backtest_item_screening_result_id"),
        "backtest_item",
        ["screening_result_id"],
    )
    op.create_unique_constraint(
        op.f("uq_backtest_item_task_security_period"),
        "backtest_item",
        ["task_id", "security_id", "screening_period_id"],
    )
    op.create_check_constraint(
        op.f("ck_backtest_item_price_version_positive"),
        "backtest_item",
        "price_version > 0",
    )

    op.create_table(
        "backtest_price_version",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("item_id", sa.UUID(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("low_strong", sa.Numeric(20, 2), nullable=False),
        sa.Column("low_watch", sa.Numeric(20, 2), nullable=False),
        sa.Column("high_watch", sa.Numeric(20, 2), nullable=False),
        sa.Column("high_strong", sa.Numeric(20, 2), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("actor_user_id", sa.String(64), nullable=False),
        sa.Column("source_version_id", sa.UUID(), nullable=True),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version_no > 0",
            name=op.f("ck_backtest_price_version_version_positive"),
        ),
        sa.CheckConstraint(
            "source IN ('SCREENING','USER','ROLLBACK','CORPORATE_ACTION')",
            name=op.f("ck_backtest_price_version_source_valid"),
        ),
        sa.CheckConstraint(
            "low_strong > 0 AND low_strong < low_watch "
            "AND low_watch < high_watch AND high_watch < high_strong",
            name=op.f("ck_backtest_price_version_prices_ordered"),
        ),
        sa.CheckConstraint(
            "low_strong <> 'NaN'::numeric "
            "AND low_strong < 'Infinity'::numeric "
            "AND low_strong > '-Infinity'::numeric "
            "AND low_watch <> 'NaN'::numeric "
            "AND low_watch < 'Infinity'::numeric "
            "AND low_watch > '-Infinity'::numeric "
            "AND high_watch <> 'NaN'::numeric "
            "AND high_watch < 'Infinity'::numeric "
            "AND high_watch > '-Infinity'::numeric "
            "AND high_strong <> 'NaN'::numeric "
            "AND high_strong < 'Infinity'::numeric "
            "AND high_strong > '-Infinity'::numeric",
            name=op.f("ck_backtest_price_version_prices_finite"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["backtest_item.id"],
            name=op.f("fk_backtest_price_version_item_id_backtest_item"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_version_id"],
            ["backtest_price_version.id"],
            name=op.f(
                "fk_backtest_price_version_source_version_id_backtest_price_version"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_price_version")),
        sa.UniqueConstraint(
            "item_id",
            "version_no",
            name=op.f("uq_backtest_price_version_item_version"),
        ),
        sa.UniqueConstraint(
            "item_id",
            "idempotency_key",
            name=op.f("uq_backtest_price_version_item_idempotency"),
        ),
    )
    op.create_index(
        op.f("ix_backtest_price_version_item_effective"),
        "backtest_price_version",
        ["item_id", "effective_date", "version_no"],
        unique=False,
    )

    op.create_table(
        "strategy_screening_control_command",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_by_user_id", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('PAUSE','RESUME','CANCEL','RETRY_FAILED')",
            name=op.f("ck_strategy_screening_control_command_action_valid"),
        ),
        sa.CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_strategy_screening_control_command_request_hash_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["strategy_screening_batch.id"],
            name=op.f(
                "fk_strategy_screening_control_command_batch_id_strategy_screening_batch"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_strategy_screening_control_command")
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name=op.f("uq_strategy_screening_control_idempotency_key"),
        ),
    )


def downgrade() -> None:
    op.drop_table("strategy_screening_control_command")
    op.drop_index(
        op.f("ix_backtest_price_version_item_effective"),
        table_name="backtest_price_version",
    )
    op.drop_constraint(
        op.f("uq_backtest_task_job_id"), "backtest_task", type_="unique"
    )
    op.drop_constraint(
        op.f("fk_backtest_task_job_id_job"),
        "backtest_task",
        type_="foreignkey",
    )
    op.drop_column("backtest_task", "job_id")
    op.drop_table("backtest_price_version")
    op.drop_constraint(
        op.f("ck_backtest_item_price_version_positive"),
        "backtest_item",
        type_="check",
    )
    op.drop_constraint(
        op.f("uq_backtest_item_task_security_period"),
        "backtest_item",
        type_="unique",
    )
    op.drop_constraint(
        op.f("uq_backtest_item_screening_result_id"),
        "backtest_item",
        type_="unique",
    )
    op.drop_constraint(
        op.f("fk_backtest_item_screening_period_id_strategy_screening_period"),
        "backtest_item",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_backtest_item_screening_result_id_strategy_screening_result"),
        "backtest_item",
        type_="foreignkey",
    )
    op.drop_column("backtest_item", "price_version")
    op.drop_column("backtest_item", "recompute_from_date")
    op.drop_column("backtest_item", "screening_period_id")
    op.drop_column("backtest_item", "screening_result_id")
    op.create_unique_constraint(
        op.f("uq_backtest_item_task_id_security_id"),
        "backtest_item",
        ["task_id", "security_id"],
    )
    op.drop_index(
        op.f("ix_backtest_task_screening_batch"), table_name="backtest_task"
    )
    op.drop_constraint(
        op.f("fk_backtest_task_screening_batch_id_strategy_screening_batch"),
        "backtest_task",
        type_="foreignkey",
    )
    op.drop_column("backtest_task", "screening_batch_id")
    op.drop_constraint(
        op.f("ck_backtest_metric_v4_amounts_valid"),
        "backtest_metric",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_backtest_metric_v4_counts_nonnegative"),
        "backtest_metric",
        type_="check",
    )
    op.drop_column("backtest_metric", "profit_factor")
    op.drop_column("backtest_metric", "net_profit_amount")
    op.drop_column("backtest_metric", "gross_loss_amount")
    op.drop_column("backtest_metric", "gross_profit_amount")
    op.drop_column("backtest_metric", "sell_count")
    op.drop_column("backtest_metric", "buy_count")
