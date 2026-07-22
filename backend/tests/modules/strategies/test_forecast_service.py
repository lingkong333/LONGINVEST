from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from long_invest.modules.strategies.contracts import (
    StrategyForecastRequest,
    TrainingDataSnapshot,
)
from long_invest.modules.strategies.forecast import (
    hash_parameter_snapshot,
    hash_source_code,
    hash_training_data_snapshot,
)
from long_invest.modules.strategies.forecast_service import (
    SandboxedStrategyForecastService,
)

SOURCE = """
STRATEGY_API_VERSION = "1.0"
STRATEGY_META = {
    "name": "test",
    "data_requirements": {"adjustment": "qfq", "min_bars": 1, "max_bars": 10},
    "parameter_schema": {"type": "object", "additionalProperties": False},
}
def calculate_targets(history, params, context):
    return {}
"""


class Runner:
    def __init__(self) -> None:
        self.payload = None

    def run(self, payload):
        self.payload = payload
        return {
            "low_strong": "8",
            "low_watch": "9",
            "high_watch": "11",
            "high_strong": "12",
        }


class Verifier:
    async def verify_forecast_request(self, request) -> bool:
        return True


class RejectingVerifier:
    async def verify_forecast_request(self, request) -> bool:
        return False


@pytest.mark.anyio
async def test_forecast_uses_only_frozen_training_request() -> None:
    training = TrainingDataSnapshot(
        security_id=uuid4(),
        symbol="600000.SH",
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 2),
        data_version=1,
        fetched_at=datetime(2026, 7, 21, tzinfo=UTC),
        source="EASTMONEY",
        price_basis="QFQ_AS_OF",
        content_hash="a" * 64,
        rows=(
            {
                "trade_date": date(2025, 1, 2),
                "open": "10",
                "high": "11",
                "low": "9",
                "close": "10",
                "volume": "1",
                "amount": "10",
            },
        ),
    )
    training = training.model_copy(
        update={"content_hash": hash_training_data_snapshot(training)}
    )
    request = StrategyForecastRequest(
        strategy_id=uuid4(),
        security_name="浦发银行",
        strategy_version_id=uuid4(),
        draft_id=None,
        draft_version=None,
        source_code=SOURCE,
        source_code_hash=hash_source_code(SOURCE),
        metadata={"name": "test"},
        parameter_schema={"type": "object", "additionalProperties": False},
        environment_version="runner-1",
        runner_image_digest="sha256:" + "d" * 64,
        parameter_snapshot={},
        parameter_hash=hash_parameter_snapshot({}),
        training_data=training,
        requested_at=datetime(2026, 7, 21, tzinfo=UTC),
    )
    runner = Runner()

    result = await SandboxedStrategyForecastService(
        runner, request_verifier=Verifier()
    ).forecast(request)

    assert str(result.values.low_watch) == "9.00"
    assert runner.payload is not None
    assert runner.payload["context"]["name"] == "浦发银行"
    assert "test_start_date" not in runner.payload["context"]


@pytest.mark.anyio
async def test_forecast_rejects_source_not_owned_by_declared_version() -> None:
    training = TrainingDataSnapshot(
        security_id=uuid4(),
        symbol="600000.SH",
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 2),
        data_version=1,
        fetched_at=datetime(2026, 7, 21, tzinfo=UTC),
        source="EASTMONEY",
        price_basis="QFQ_AS_OF",
        content_hash="a" * 64,
        rows=(
            {
                "trade_date": date(2025, 1, 2),
                "open": "10",
                "high": "11",
                "low": "9",
                "close": "10",
                "volume": "1",
                "amount": "10",
            },
        ),
    )
    training = training.model_copy(
        update={"content_hash": hash_training_data_snapshot(training)}
    )
    request = StrategyForecastRequest(
        strategy_id=uuid4(),
        security_name="浦发银行",
        strategy_version_id=uuid4(),
        draft_id=None,
        draft_version=None,
        source_code=SOURCE,
        source_code_hash=hash_source_code(SOURCE),
        metadata={"name": "test"},
        parameter_schema={"type": "object", "additionalProperties": False},
        environment_version="runner-1",
        runner_image_digest="sha256:" + "d" * 64,
        parameter_snapshot={},
        parameter_hash=hash_parameter_snapshot({}),
        training_data=training,
        requested_at=datetime(2026, 7, 21, tzinfo=UTC),
    )
    runner = Runner()

    with pytest.raises(RuntimeError, match="not owned"):
        await SandboxedStrategyForecastService(
            runner, request_verifier=RejectingVerifier()
        ).forecast(request)

    assert runner.payload is None


def test_training_content_hash_does_not_depend_on_fetch_time() -> None:
    values = {
        "security_id": uuid4(),
        "symbol": "600000.SH",
        "start_date": date(2025, 1, 2),
        "end_date": date(2025, 1, 2),
        "data_version": 1,
        "source": "EASTMONEY",
        "price_basis": "QFQ_AS_OF",
        "content_hash": "a" * 64,
        "rows": (
            {
                "trade_date": date(2025, 1, 2),
                "open": "10",
                "high": "11",
                "low": "9",
                "close": "10",
            },
        ),
    }
    first = TrainingDataSnapshot(**values, fetched_at=datetime(2026, 7, 21, tzinfo=UTC))
    second = TrainingDataSnapshot(
        **values, fetched_at=datetime(2026, 7, 22, tzinfo=UTC)
    )

    assert hash_training_data_snapshot(first) == hash_training_data_snapshot(second)
