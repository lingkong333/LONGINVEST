import hashlib
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.bootstrap.strategy_validation import (
    StrategyValidationEvidenceVerifier,
    StrategyValidationExecutor,
)
from long_invest.modules.backtests.contracts import BacktestItemStatus, BacktestMode
from long_invest.modules.strategies.contracts import ValidationEvidenceClaim
from long_invest.modules.strategies.forecast import (
    hash_parameter_snapshot,
    hash_source_code,
)

SOURCE = """
STRATEGY_API_VERSION = "1.0"
STRATEGY_META = {
    "name": "validation fixture",
    "data_requirements": {"adjustment": "qfq", "min_bars": 2, "max_bars": 100},
    "parameter_schema": {
        "type": "object",
        "properties": {"window": {"type": "integer"}},
        "required": ["window"],
        "additionalProperties": False,
    },
}
def calculate_targets(history, params, context):
    return {"low_strong": 8, "low_watch": 9, "high_watch": 11, "high_strong": 12}
"""


def _hash_json(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def fixture():
    validation_id = uuid4()
    strategy_id = uuid4()
    draft_id = uuid4()
    task_id = uuid4()
    item_id = uuid4()
    security_id = uuid4()
    metadata = {
        "name": "validation fixture",
        "data_requirements": {
            "adjustment": "qfq",
            "min_bars": 2,
            "max_bars": 100,
        },
        "parameter_schema": {
            "type": "object",
            "properties": {"window": {"type": "integer"}},
            "required": ["window"],
            "additionalProperties": False,
        },
    }
    params = {"window": 20}
    source_hash = hash_source_code(SOURCE)
    environment = "python-3.12"
    digest = f"sha256:{'a' * 64}"
    evidence = {
        "schema_version": 1,
        "source_code_hash": source_hash,
        "metadata": metadata,
        "metadata_hash": _hash_json(metadata),
        "parameter_schema": metadata["parameter_schema"],
        "parameter_schema_hash": _hash_json(metadata["parameter_schema"]),
        "params": params,
        "parameter_hash": hash_parameter_snapshot(params),
        "environment_version": environment,
        "environment_hash": hashlib.sha256(environment.encode()).hexdigest(),
        "runner_image_digest": digest,
        "checks": {},
    }
    run = SimpleNamespace(
        id=validation_id,
        strategy_id=strategy_id,
        draft_version=3,
        source_code_hash=source_hash,
        evidence_snapshot=evidence,
    )
    draft = SimpleNamespace(
        id=draft_id,
        strategy_id=strategy_id,
        draft_version=3,
        source_code=SOURCE,
    )
    forecast = SimpleNamespace(
        training_start_date=date(2010, 1, 1),
        training_end_date=date(2020, 12, 31),
        training_data_hash="b" * 64,
        source_code_hash=source_hash,
        parameter_hash=evidence["parameter_hash"],
        environment_version=environment,
        runner_image_digest=digest,
    )
    test = SimpleNamespace(
        start_date=date(2021, 1, 1),
        end_date=date(2022, 12, 31),
        data_hash="c" * 64,
    )
    task = SimpleNamespace(
        mode=BacktestMode.SINGLE,
        draft_id=draft_id,
        draft_version=3,
        draft_source_code=SOURCE,
        source_code_hash=source_hash,
        strategy_metadata=metadata,
        parameter_schema=metadata["parameter_schema"],
        parameter_snapshot=params,
        parameter_hash=evidence["parameter_hash"],
        environment_version=environment,
        runner_image_digest=digest,
        date_range=SimpleNamespace(
            training_start_date=date(2010, 1, 1),
            training_end_date=date(2020, 12, 31),
            test_start_date=date(2021, 1, 1),
            test_end_date=date(2022, 12, 31),
        ),
        universe_snapshot=(SimpleNamespace(security_id=security_id),),
    )
    state = SimpleNamespace(
        task=task,
        item_id=item_id,
        item_status=BacktestItemStatus.SUCCEEDED,
    )
    result = SimpleNamespace(
        item_id=item_id,
        item_status=BacktestItemStatus.SUCCEEDED,
        forecast=forecast,
        test_data_snapshot=test,
        metric=SimpleNamespace(total_return=1),
    )
    strategies = SimpleNamespace(
        get_validation_run=_async_return(run),
        get_draft=_async_return(draft),
    )
    backtests = SimpleNamespace(
        get_execution=_async_return(state),
        get_result=_async_return(result),
    )
    forecasts = RecordingForecasts()
    return SimpleNamespace(
        run=run,
        draft=draft,
        task_id=task_id,
        strategies=strategies,
        backtests=backtests,
        forecasts=forecasts,
        state=state,
        result=result,
    )


def _async_return(value):
    async def method(*_args, **_kwargs):
        return value

    return method


class RecordingForecasts:
    def __init__(self, error=None):
        self.requests = []
        self.error = error

    async def forecast(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return SimpleNamespace()


@pytest.mark.anyio
async def test_validation_runs_fixed_sample_and_binds_completed_backtest():
    values = fixture()
    subject = StrategyValidationExecutor(
        strategies=values.strategies,
        backtests=values.backtests,
        forecasts=values.forecasts,
        clock=lambda: datetime(2026, 7, 22, tzinfo=UTC),
    )

    outcome = await subject.execute(values.run.id, values.task_id)

    assert outcome.succeeded
    assert set(outcome.evidence_snapshot) == {
        "static_analysis",
        "fixed_sample",
        "specified_stock",
        "holdout_backtest",
    }
    assert outcome.evidence_snapshot["holdout_backtest"]["task_id"] == str(
        values.task_id
    )
    assert outcome.evidence_snapshot["holdout_backtest"]["test_data_hash"] == "c" * 64
    assert len(values.forecasts.requests[0].training_data.rows) == 2

    verifier = StrategyValidationEvidenceVerifier(backtests=values.backtests)
    claim = ValidationEvidenceClaim(
        validation_run_id=values.run.id,
        strategy_id=values.run.strategy_id,
        draft_version=values.run.draft_version,
        source_code_hash=values.run.source_code_hash,
        metadata_hash=values.run.evidence_snapshot["metadata_hash"],
        parameter_schema_hash=values.run.evidence_snapshot["parameter_schema_hash"],
        parameter_hash=values.run.evidence_snapshot["parameter_hash"],
        environment_hash=values.run.evidence_snapshot["environment_hash"],
        runner_image_digest=values.run.evidence_snapshot["runner_image_digest"],
        checks=outcome.evidence_snapshot,
    )
    assert await verifier.verify(claim)


@pytest.mark.anyio
async def test_validation_rejects_backtest_for_another_draft():
    values = fixture()
    values.state.task.draft_id = uuid4()
    subject = StrategyValidationExecutor(
        strategies=values.strategies,
        backtests=values.backtests,
        forecasts=values.forecasts,
    )

    outcome = await subject.execute(values.run.id, values.task_id)

    assert not outcome.succeeded
    assert outcome.error_code == "STRATEGY_VALIDATION_FACTS_MISMATCH"
    assert values.forecasts.requests == []


@pytest.mark.anyio
async def test_validation_records_fixed_sample_failure_without_backtest_reuse():
    values = fixture()
    values.forecasts.error = TimeoutError()
    subject = StrategyValidationExecutor(
        strategies=values.strategies,
        backtests=values.backtests,
        forecasts=values.forecasts,
    )

    outcome = await subject.execute(values.run.id, values.task_id)

    assert not outcome.succeeded
    assert outcome.error_code == "STRATEGY_VALIDATION_FIXED_SAMPLE_FAILED"
