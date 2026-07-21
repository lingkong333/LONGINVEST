from uuid import uuid4

from sqlalchemy.dialects import postgresql

from long_invest.modules.strategies.repository import StrategyRepository


def sql(statement) -> str:
    return " ".join(
        str(statement.compile(dialect=postgresql.dialect())).split()
    ).lower()


def test_list_statement_is_stably_paginated_and_hides_archived_by_default():
    statement = StrategyRepository.list_statement(
        page=2, page_size=20, include_archived=False
    )
    rendered = sql(statement)

    assert "strategy.status !=" in rendered
    assert "order by strategy.name, strategy.id" in rendered
    assert "limit" in rendered and "offset" in rendered


def test_draft_update_uses_expected_version_condition():
    statement = StrategyRepository.update_draft_statement(
        uuid4(), source_code="changed", expected_version=4
    )
    rendered = sql(statement)

    assert "strategy_draft.draft_version =" in rendered
    assert "draft_version=(strategy_draft.draft_version +" in rendered
    assert "returning" in rendered


def test_publish_queries_lock_owned_strategy_rows():
    statement = StrategyRepository.strategy_statement(uuid4(), for_update=True)

    assert "for update" in sql(statement)
