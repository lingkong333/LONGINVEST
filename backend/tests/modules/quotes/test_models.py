from sqlalchemy import CheckConstraint, UniqueConstraint

from long_invest.modules.quotes.models import QuoteCycle, QuoteCycleItem


def test_quote_models_have_stable_database_constraints() -> None:
    cycle_constraints = {
        constraint.name for constraint in QuoteCycle.__table__.constraints
    }
    item_constraints = {
        constraint.name for constraint in QuoteCycleItem.__table__.constraints
    }
    assert "uq_quote_cycle_idempotency" in cycle_constraints
    assert "ck_quote_cycle_deadline" in cycle_constraints
    assert "uq_quote_cycle_item_symbol" in item_constraints
    assert any(
        isinstance(c, UniqueConstraint) for c in QuoteCycle.__table__.constraints
    )
    assert any(
        isinstance(c, CheckConstraint) for c in QuoteCycleItem.__table__.constraints
    )


def test_quote_item_persists_standard_quote_and_quality_fields() -> None:
    columns = set(QuoteCycleItem.__table__.columns.keys())
    assert {
        "symbol",
        "price",
        "open",
        "high",
        "low",
        "previous_close",
        "volume",
        "amount",
        "quote_time",
        "received_at",
        "provider",
        "status",
        "error_code",
        "conflict_evidence",
        "eligible_for_evaluation",
    } <= columns
