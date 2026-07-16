from sqlalchemy import UniqueConstraint, event

from long_invest.modules.positions.models import UserPosition, UserPositionHistory


def test_position_current_and_history_table_shapes() -> None:
    assert UserPosition.__tablename__ == "user_position"
    assert UserPositionHistory.__tablename__ == "user_position_history"
    assert UserPosition.__table__.c.latest_history_id.nullable is True
    assert {
        key.target_fullname
        for key in UserPosition.__table__.c.latest_history_id.foreign_keys
    } == {"user_position_history.id"}
    assert "updated_at" not in UserPositionHistory.__table__.c
    assert not UserPosition.__mapper__.relationships
    assert not UserPositionHistory.__mapper__.relationships
    assert event.contains(
        UserPositionHistory,
        "before_update",
        UserPositionHistory._reject_mutation,
    )


def test_position_security_and_history_version_are_unique() -> None:
    current_uniques = {
        tuple(column.name for column in item.columns)
        for item in UserPosition.__table__.constraints
        if isinstance(item, UniqueConstraint)
    }
    history_uniques = {
        tuple(column.name for column in item.columns)
        for item in UserPositionHistory.__table__.constraints
        if isinstance(item, UniqueConstraint)
    }
    assert ("security_id",) in current_uniques
    assert ("security_id", "position_version") in history_uniques
    assert "uq_user_position_history_security_version" in {
        item.name for item in UserPositionHistory.__table__.constraints
    }
    assert "ck_user_position_status_valid" in {
        item.name for item in UserPosition.__table__.constraints
    }
    assert UserPosition.__table__.c.updated_at.type.timezone is True
