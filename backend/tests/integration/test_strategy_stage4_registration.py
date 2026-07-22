from long_invest.bootstrap.app import create_app
from long_invest.entrypoints import job_worker
from long_invest.modules.strategies.jobs import strategy_publish, strategy_validate
from long_invest.modules.targets.jobs import target_calculate


def test_strategy_routes_are_registered_in_the_main_application() -> None:
    paths = create_app().openapi()["paths"]

    assert "/api/v1/strategies" in paths
    assert "/api/v1/strategies/{strategy_id}/validate" in paths
    assert "/api/v1/strategies/{strategy_id}/publish" in paths
    assert "/api/v1/backtests" in paths
    assert "/api/v1/backtests/{task_id}" in paths


def test_strategy_jobs_are_registered_in_the_shared_worker() -> None:
    assert job_worker.HANDLERS["STRATEGY_VALIDATE"] is strategy_validate
    assert job_worker.HANDLERS["STRATEGY_PUBLISH"] is strategy_publish
    assert job_worker.HANDLERS["TARGET_CALCULATE"] is target_calculate
    assert callable(job_worker.HANDLERS["BACKTEST_SINGLE"])
