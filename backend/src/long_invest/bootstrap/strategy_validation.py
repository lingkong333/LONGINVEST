from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

from long_invest.modules.backtests.contracts import BacktestItemStatus, BacktestMode
from long_invest.modules.strategies.contracts import (
    StrategyForecastRequest,
    TrainingDataSnapshot,
    ValidationEvidenceClaim,
)
from long_invest.modules.strategies.forecast import hash_training_data_snapshot
from long_invest.modules.strategies.jobs import StrategyValidationOutcome
from long_invest.modules.strategies.static_analysis import analyze_strategy_source
from long_invest.platform.json_snapshot import thaw_json_value

_FIXED_SAMPLE_LIMIT = 10_000


class StrategyValidationExecutor:
    def __init__(
        self,
        *,
        strategies: Any,
        backtests: Any,
        forecasts: Any,
        clock: Any = lambda: datetime.now(UTC),
    ) -> None:
        self._strategies = strategies
        self._backtests = backtests
        self._forecasts = forecasts
        self._clock = clock

    async def execute(
        self, validation_run_id: UUID, backtest_task_id: UUID
    ) -> StrategyValidationOutcome:
        try:
            run = await self._strategies.get_validation_run(validation_run_id)
            draft = await self._strategies.get_draft(run.strategy_id)
            analysis = analyze_strategy_source(draft.source_code)
            state, result = await _load_backtest(self._backtests, backtest_task_id)
            fixed = _fixed_training_snapshot(analysis, self._clock())
            facts = _validated_facts(run, draft, analysis, state, result)
        except Exception:
            return _failed("STRATEGY_VALIDATION_FACTS_MISMATCH")

        try:
            await self._forecasts.forecast(
                StrategyForecastRequest(
                    strategy_id=run.strategy_id,
                    security_name="固定验证样本",
                    draft_id=draft.id,
                    draft_version=draft.draft_version,
                    source_code=draft.source_code,
                    source_code_hash=run.source_code_hash,
                    metadata=analysis.metadata,
                    parameter_schema=analysis.parameter_schema,
                    environment_version=facts["environment_version"],
                    runner_image_digest=facts["runner_image_digest"],
                    parameter_snapshot=facts["params"],
                    parameter_hash=facts["parameter_hash"],
                    training_data=fixed,
                    requested_at=self._clock(),
                )
            )
        except Exception:
            return _failed("STRATEGY_VALIDATION_FIXED_SAMPLE_FAILED")

        return StrategyValidationOutcome(
            succeeded=True,
            evidence_snapshot=_checks(
                validation_run_id, backtest_task_id, facts, fixed, state, result
            ),
        )


class StrategyValidationEvidenceVerifier:
    def __init__(self, *, backtests: Any) -> None:
        self._backtests = backtests

    async def verify(self, claim: ValidationEvidenceClaim) -> bool:
        try:
            checks = thaw_json_value(claim.checks)
            task_id = UUID(checks["holdout_backtest"]["task_id"])
            state, result = await _load_backtest(self._backtests, task_id)
            if task_id != UUID(checks["specified_stock"]["task_id"]):
                return False
            task = state.task
            forecast = result.forecast
            test = result.test_data_snapshot
            if forecast is None or test is None:
                return False
            common = {
                "source_code_hash": claim.source_code_hash,
                "metadata_hash": claim.metadata_hash,
                "parameter_schema_hash": claim.parameter_schema_hash,
                "parameter_hash": claim.parameter_hash,
                "environment_hash": claim.environment_hash,
                "runner_image_digest": claim.runner_image_digest,
            }
            expected_static = _common_check(
                claim.validation_run_id,
                uuid5(claim.validation_run_id, "static-analysis-task"),
                uuid5(claim.validation_run_id, "static-analysis-snapshot"),
                common,
            )
            fixed = checks["fixed_sample"]
            expected_fixed_common = _common_check(
                claim.validation_run_id,
                uuid5(claim.validation_run_id, "fixed-sample-task"),
                uuid5(claim.validation_run_id, "fixed-sample-snapshot"),
                common,
            )
            expected_holdout = {
                **_common_check(
                    claim.validation_run_id, task_id, result.item_id, common
                ),
                "training_start": forecast.training_start_date.isoformat(),
                "training_end": forecast.training_end_date.isoformat(),
                "training_data_hash": forecast.training_data_hash,
                "security_id": str(task.universe_snapshot[0].security_id),
                "test_start": test.start_date.isoformat(),
                "test_end": test.end_date.isoformat(),
                "test_data_hash": test.data_hash,
            }
            return (
                task.mode is BacktestMode.SINGLE
                and task.draft_version == claim.draft_version
                and task.source_code_hash == claim.source_code_hash
                and task.parameter_hash == claim.parameter_hash
                and _hash_json(task.strategy_metadata) == claim.metadata_hash
                and _hash_json(task.parameter_schema) == claim.parameter_schema_hash
                and hashlib.sha256(task.environment_version.encode()).hexdigest()
                == claim.environment_hash
                and task.runner_image_digest == claim.runner_image_digest
                and checks["static_analysis"] == expected_static
                and {
                    key: value
                    for key, value in fixed.items()
                    if key
                    not in {
                        "training_start",
                        "training_end",
                        "training_data_hash",
                    }
                }
                == expected_fixed_common
                and checks["holdout_backtest"] == expected_holdout
                and checks["specified_stock"]
                == {
                    key: value
                    for key, value in expected_holdout.items()
                    if key not in {"test_start", "test_end", "test_data_hash"}
                }
            )
        except Exception:
            return False


async def _load_backtest(backtests: Any, task_id: UUID):
    state = await backtests.get_execution(task_id)
    result = await backtests.get_result(task_id, state.item_id)
    if (
        state.item_status is not BacktestItemStatus.SUCCEEDED
        or result.item_status is not BacktestItemStatus.SUCCEEDED
        or result.forecast is None
        or result.test_data_snapshot is None
        or result.metric is None
    ):
        raise ValueError("backtest is incomplete")
    return state, result


def _validated_facts(run: Any, draft: Any, analysis: Any, state: Any, result: Any):
    evidence = thaw_json_value(run.evidence_snapshot)
    task = state.task
    forecast = result.forecast
    test = result.test_data_snapshot
    if forecast is None or test is None:
        raise ValueError("backtest snapshots are incomplete")
    expected_metadata = thaw_json_value(analysis.metadata)
    expected_schema = thaw_json_value(analysis.parameter_schema)
    if not all(
        (
            draft.draft_version == run.draft_version,
            task.mode is BacktestMode.SINGLE,
            task.draft_id == draft.id,
            task.draft_version == run.draft_version,
            task.draft_source_code == draft.source_code,
            task.source_code_hash == run.source_code_hash,
            thaw_json_value(task.strategy_metadata) == expected_metadata,
            thaw_json_value(task.parameter_schema) == expected_schema,
            thaw_json_value(task.parameter_snapshot) == evidence["params"],
            task.parameter_hash == evidence["parameter_hash"],
            task.environment_version == evidence["environment_version"],
            task.runner_image_digest == evidence["runner_image_digest"],
            forecast.source_code_hash == run.source_code_hash,
            forecast.parameter_hash == evidence["parameter_hash"],
            forecast.environment_version == evidence["environment_version"],
            forecast.runner_image_digest == evidence["runner_image_digest"],
            forecast.training_start_date == task.date_range.training_start_date,
            forecast.training_end_date == task.date_range.training_end_date,
            test.start_date == task.date_range.test_start_date,
            test.end_date == task.date_range.test_end_date,
            forecast.training_end_date < test.start_date,
            evidence["source_code_hash"] == run.source_code_hash,
            evidence["metadata"] == expected_metadata,
            evidence["metadata_hash"] == _hash_json(expected_metadata),
            evidence["parameter_schema"] == expected_schema,
            evidence["parameter_schema_hash"] == _hash_json(expected_schema),
            evidence["parameter_hash"] == _hash_json(evidence["params"]),
            evidence["environment_hash"]
            == hashlib.sha256(evidence["environment_version"].encode()).hexdigest(),
        )
    ):
        raise ValueError("validation facts do not match")
    return evidence


def _fixed_training_snapshot(analysis: Any, fetched_at: datetime):
    min_bars = int(analysis.metadata["data_requirements"]["min_bars"])
    if min_bars > _FIXED_SAMPLE_LIMIT:
        raise ValueError("fixed sample is too large")
    start = date(1990, 1, 1)
    rows = []
    for index in range(min_bars):
        trade_date = start + timedelta(days=index)
        close = Decimal("10") + Decimal(index) / Decimal("100")
        rows.append(
            {
                "trade_date": trade_date,
                "open": close,
                "high": close + Decimal("0.10"),
                "low": close - Decimal("0.10"),
                "close": close,
                "volume": Decimal("100000"),
                "amount": close * Decimal("100000"),
            }
        )
    snapshot = TrainingDataSnapshot(
        security_id=uuid5(UUID(int=0), "longinvest:fixed-validation-security"),
        symbol="000001.SZ",
        start_date=start,
        end_date=rows[-1]["trade_date"],
        data_version=1,
        fetched_at=fetched_at,
        source="FIXED_SAMPLE",
        price_basis="QFQ_AS_OF",
        content_hash="0" * 64,
        rows=tuple(rows),
    )
    return snapshot.model_copy(
        update={"content_hash": hash_training_data_snapshot(snapshot)}
    )


def _checks(run_id: UUID, task_id: UUID, facts: dict[str, Any], fixed, state, result):
    common = {
        key: facts[key]
        for key in (
            "source_code_hash",
            "metadata_hash",
            "parameter_schema_hash",
            "parameter_hash",
            "environment_hash",
            "runner_image_digest",
        )
    }
    task = state.task
    forecast = result.forecast
    test = result.test_data_snapshot
    security_id = str(task.universe_snapshot[0].security_id)
    fixed_common = _common_check(
        run_id,
        uuid5(run_id, "fixed-sample-task"),
        uuid5(run_id, "fixed-sample-snapshot"),
        common,
    )
    backtest_common = _common_check(run_id, task_id, result.item_id, common)
    training = {
        "training_start": forecast.training_start_date.isoformat(),
        "training_end": forecast.training_end_date.isoformat(),
        "training_data_hash": forecast.training_data_hash,
    }
    return {
        "static_analysis": _common_check(
            run_id,
            uuid5(run_id, "static-analysis-task"),
            uuid5(run_id, "static-analysis-snapshot"),
            common,
        ),
        "fixed_sample": {
            **fixed_common,
            "training_start": fixed.start_date.isoformat(),
            "training_end": fixed.end_date.isoformat(),
            "training_data_hash": fixed.content_hash,
        },
        "specified_stock": {**backtest_common, **training, "security_id": security_id},
        "holdout_backtest": {
            **backtest_common,
            **training,
            "security_id": security_id,
            "test_start": test.start_date.isoformat(),
            "test_end": test.end_date.isoformat(),
            "test_data_hash": test.data_hash,
        },
    }


def _common_check(run_id: UUID, task_id: UUID, snapshot_id: UUID, facts):
    return {
        "run_id": str(run_id),
        "task_id": str(task_id),
        "snapshot_id": str(snapshot_id),
        "status": "SUCCEEDED",
        **facts,
    }


def _hash_json(value: Any) -> str:
    payload = json.dumps(
        thaw_json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _failed(code: str) -> StrategyValidationOutcome:
    return StrategyValidationOutcome(False, {}, code)
