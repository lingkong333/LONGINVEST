import pytest
from sqlalchemy import column, select, table
from sqlalchemy.dialects import postgresql

from long_invest.platform.database.pagination import PageWindow


def test_page_window_applies_bounded_offset_and_limit() -> None:
    securities = table("security", column("symbol"))
    statement = PageWindow(page=3, page_size=20).apply(
        select(securities.c.symbol).order_by(securities.c.symbol)
    )

    compiled = statement.compile(dialect=postgresql.dialect())

    assert compiled.params["param_1"] == 20
    assert compiled.params["param_2"] == 40
    assert "ORDER BY security.symbol" in str(compiled)


@pytest.mark.parametrize(
    ("page", "page_size"),
    [(0, 20), (1, 0), (1, 201)],
)
def test_page_window_rejects_unbounded_values(page: int, page_size: int) -> None:
    with pytest.raises(ValueError):
        PageWindow(page=page, page_size=page_size)
