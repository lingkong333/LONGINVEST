import pytest

from long_invest.entrypoints.bulk_backtest_worker import _concurrency


@pytest.mark.parametrize("value", ["1", "4", "8"])
def test_bulk_backtest_concurrency_accepts_bounded_values(value) -> None:
    assert _concurrency(value) == int(value)


@pytest.mark.parametrize("value", ["0", "9", "invalid"])
def test_bulk_backtest_concurrency_rejects_unbounded_values(value) -> None:
    with pytest.raises(ValueError):
        _concurrency(value)
