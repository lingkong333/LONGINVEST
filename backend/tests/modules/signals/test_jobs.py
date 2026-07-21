from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from long_invest.modules.signals.jobs import SignalJobApplication
from long_invest.platform.errors import AppError


@pytest.mark.anyio
async def test_batch_keeps_processing_after_one_stock_fails() -> None:
    first, second, subscription_id = uuid4(), uuid4(), uuid4()
    application = SignalJobApplication(None)
    application._evaluate_quote_item = AsyncMock(
        side_effect=[
            SimpleNamespace(
                code="SIGNAL_EVALUATED",
                evaluation=SimpleNamespace(subscription_id=subscription_id),
            ),
            AppError(
                code="SIGNAL_INPUT_SUPERSEDED",
                message="stale",
                status_code=409,
            ),
        ]
    )

    report = await application.evaluate_batch(
        cycle_id=uuid4(),
        item_ids=(first, second),
        request_id="request-1",
    )

    assert (report.succeeded, report.failed) == (1, 1)
    assert [item.code for item in report.items] == [
        "SIGNAL_EVALUATED",
        "SIGNAL_INPUT_SUPERSEDED",
    ]
    assert application._evaluate_quote_item.await_count == 2
