import pytest

from long_invest.platform.jobs.contracts import SubmitPostgresJob


def command(**overrides) -> SubmitPostgresJob:
    values = {
        "job_type": "TEST_JOB",
        "module_owner": "tests",
        "idempotency_scope": "tests:postgres-job",
        "idempotency_key": "same",
        "request_id": "req_test",
        "config_snapshot": {"value": 1},
    }
    values.update(overrides)
    return SubmitPostgresJob(**values)


def test_submit_postgres_job_copies_mutable_config() -> None:
    config = {"items": [1]}
    submitted = command(config_snapshot=config)

    config["items"].append(2)

    assert submitted.config_snapshot == {"items": [1]}


@pytest.mark.parametrize("priority", [-1, 4])
def test_submit_postgres_job_rejects_invalid_priority(priority: int) -> None:
    with pytest.raises(ValueError, match="priority"):
        command(priority=priority)


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_attempts": 0},
        {"max_recoveries": -1},
        {"soft_timeout_seconds": 61, "hard_timeout_seconds": 60},
        {"config_snapshot": {"invalid": float("nan")}},
    ],
)
def test_submit_postgres_job_rejects_invalid_limits(overrides) -> None:
    with pytest.raises(ValueError):
        command(**overrides)
