from sqlalchemy import UniqueConstraint

from long_invest.modules.watchlists.models import Watchlist, WatchlistItem


def test_watchlist_tables_and_member_uniqueness() -> None:
    assert Watchlist.__tablename__ == "watchlist"
    assert WatchlistItem.__tablename__ == "watchlist_item"
    assert Watchlist.__table__.c.version.nullable is False
    assert Watchlist.__table__.c.archived_at.nullable is True
    assert Watchlist.__table__.c.owner_user_id.nullable is False
    assert {
        key.target_fullname for key in Watchlist.__table__.c.owner_user_id.foreign_keys
    } == {"app_user.id"}
    uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in WatchlistItem.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("watchlist_id", "security_id") in uniques
    assert not Watchlist.__mapper__.relationships
    assert not WatchlistItem.__mapper__.relationships


def test_watchlist_model_uses_uuid_timestamptz_and_positive_checks() -> None:
    assert str(Watchlist.__table__.c.id.type) == "UUID"
    assert Watchlist.__table__.c.created_at.type.timezone is True
    checks = {constraint.name for constraint in Watchlist.__table__.constraints}
    assert "ck_watchlist_version_positive" in checks
    assert "ck_watchlist_display_order_nonnegative" in checks
