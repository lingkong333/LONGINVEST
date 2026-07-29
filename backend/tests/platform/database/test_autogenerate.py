from sqlalchemy import Column, ForeignKeyConstraint, Integer, MetaData, Table

from long_invest.platform.database.autogenerate import (
    include_database_name,
    include_schema_object,
)


def test_yearly_daily_bar_partition_is_excluded() -> None:
    assert not include_database_name(
        "daily_bar_unadjusted_2026",
        "table",
        {"schema_name": None},
    )


def test_parent_and_similarly_named_tables_are_included() -> None:
    assert include_database_name("daily_bar_unadjusted", "table", {})
    assert include_database_name("daily_bar_unadjusted_archive", "table", {})
    assert include_database_name("daily_bar_unadjusted_26", "table", {})


def test_non_table_names_are_included() -> None:
    assert include_database_name("daily_bar_unadjusted_2026", "index", {})


def test_inherited_partition_foreign_key_is_excluded() -> None:
    metadata = MetaData()
    Table(
        "daily_bar_unadjusted_2026",
        metadata,
        Column("security_id", Integer, primary_key=True),
    )
    revision = Table(
        "daily_bar_revision",
        metadata,
        Column("daily_bar_security_id", Integer),
        ForeignKeyConstraint(
            ["daily_bar_security_id"],
            ["daily_bar_unadjusted_2026.security_id"],
        ),
    )

    constraint = next(iter(revision.foreign_key_constraints))
    assert not include_schema_object(
        constraint,
        constraint.name,
        "foreign_key_constraint",
        True,
        None,
    )


def test_normal_or_model_foreign_keys_are_included() -> None:
    metadata = MetaData()
    Table("security", metadata, Column("id", Integer, primary_key=True))
    revision = Table(
        "daily_bar_revision",
        metadata,
        Column("security_id", Integer),
        ForeignKeyConstraint(["security_id"], ["security.id"]),
    )
    constraint = next(iter(revision.foreign_key_constraints))

    assert include_schema_object(
        constraint,
        constraint.name,
        "foreign_key_constraint",
        True,
        None,
    )
    assert include_schema_object(
        constraint,
        constraint.name,
        "foreign_key_constraint",
        False,
        None,
    )
