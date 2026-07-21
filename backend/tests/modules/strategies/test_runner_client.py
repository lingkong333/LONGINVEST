from __future__ import annotations

import io
import json
import tarfile
from typing import Any

import pytest

from long_invest.modules.strategies.runner_client import (
    DockerStrategyRunnerClient,
    StrategyRunnerFailure,
)

IMAGE = "long-invest-strategy-runner@sha256:" + "a" * 64
SUCCESS_OUTPUT = (
    b'{"low_strong":"1","low_watch":"2",'
    b'"high_watch":"3","high_strong":"4"}'
)


class FakeContainer:
    def __init__(
        self,
        *,
        stdout: bytes = SUCCESS_OUTPUT,
        stderr: bytes = b"",
        status_code: int = 0,
        oom_killed: bool = False,
        wait_error: Exception | None = None,
        archive_error: Exception | None = None,
        remove_error: Exception | None = None,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.status_code = status_code
        self.wait_error = wait_error
        self.archive_error = archive_error
        self.remove_error = remove_error
        self.attrs = {"State": {"OOMKilled": oom_killed}}
        self.id = "container-1"
        self.events: list[str] = []
        self.archive: bytes | None = None
        self.started = False
        self.killed = False
        self.removed = False
        self.wait_timeout: float | None = None

    def put_archive(self, path: str, data: bytes) -> None:
        assert path == "/tmp"
        self.events.append("put_archive")
        if self.archive_error:
            raise self.archive_error
        self.archive = data

    def start(self) -> None:
        self.events.append("start")
        self.started = True

    def wait(self, *, timeout: float) -> dict[str, int]:
        self.events.append("wait")
        self.wait_timeout = timeout
        if self.wait_error:
            raise self.wait_error
        return {"StatusCode": self.status_code}

    def reload(self) -> None:
        return None

    def logs(self, *, stdout: bool, stderr: bool) -> bytes:
        return self.stdout if stdout and not stderr else self.stderr

    def kill(self) -> None:
        self.events.append("kill")
        self.killed = True

    def remove(self, *, force: bool) -> None:
        assert force is True
        self.events.append("remove")
        if self.remove_error:
            error = self.remove_error
            self.remove_error = None
            raise error
        self.removed = True


class FakeContainers:
    def __init__(self, container: FakeContainer) -> None:
        self.container = container
        self.create_kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> FakeContainer:
        self.create_kwargs = kwargs
        return self.container

    def get(self, container_id: str) -> FakeContainer:
        assert container_id == self.container.id
        return self.container


class FakeDockerClient:
    def __init__(self, container: FakeContainer) -> None:
        self.containers = FakeContainers(container)
        self.api = type("FakeApi", (), {"timeout": 60.0})()


def _payload() -> dict[str, object]:
    return {
        "source_code": "def calculate_targets(history, params, context): return {}",
        "parameters": {},
        "context": {
            "symbol": "600000.SH",
            "exchange": "SH",
            "name": "浦发银行",
            "as_of_date": "2025-12-31",
            "strategy_version_id": "version-id",
            "data_version": 1,
            "calculation_reason": "manual",
        },
        "history": [
            {
                "trade_date": "2025-12-31",
                "open": "10",
                "high": "11",
                "low": "9",
                "close": "10",
                "volume": "100",
                "amount": "1000",
            }
        ],
    }


def _client(
    container: FakeContainer, **kwargs: Any
) -> tuple[DockerStrategyRunnerClient, FakeDockerClient]:
    docker_client = FakeDockerClient(container)
    client = DockerStrategyRunnerClient(
        docker_client=docker_client,
        image=IMAGE,
        seccomp_profile="/etc/long-invest/seccomp.json",
        **kwargs,
    )
    return client, docker_client


def test_runner_uses_one_shot_hardened_container_and_always_removes_it() -> None:
    container = FakeContainer()
    client, docker_client = _client(container)

    result = client.run(_payload())

    assert result["low_strong"] == "1"
    assert container.started is True
    assert container.removed is True
    assert container.events[:3] == ["start", "put_archive", "wait"]
    assert docker_client.api.timeout <= 10
    options = docker_client.containers.create_kwargs
    assert options is not None
    assert options["image"] == IMAGE
    assert options["network_disabled"] is True
    assert options["network_mode"] == "none"
    assert options["read_only"] is True
    assert options["user"] == "65532:65532"
    assert options["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in options["security_opt"]
    assert "seccomp=/etc/long-invest/seccomp.json" in options["security_opt"]
    assert options["nano_cpus"] == 1_000_000_000
    assert options["mem_limit"] == "512m"
    assert options["memswap_limit"] == "512m"
    assert options["pids_limit"] == 32
    assert options["tmpfs"] == {"/tmp": "rw,noexec,nosuid,nodev,size=64m"}

    assert container.archive is not None
    with tarfile.open(fileobj=io.BytesIO(container.archive), mode="r:") as archive:
        member = archive.getmember("input.json")
        archived_payload = json.load(archive.extractfile(member))
    assert archived_payload == _payload()
    assert member.uid == 65532
    assert member.gid == 65532
    assert member.mode == 0o400


def test_runner_timeout_is_clamped_to_ten_seconds_and_cleans_up() -> None:
    container = FakeContainer(wait_error=TimeoutError())
    client, _ = _client(container, timeout_seconds=30)

    with pytest.raises(StrategyRunnerFailure) as error:
        client.run(_payload())

    assert error.value.code == "STRATEGY_FORECAST_TIMEOUT"
    assert container.wait_timeout is not None
    assert 0 < container.wait_timeout <= 10
    assert container.killed is True
    assert container.removed is True


@pytest.mark.parametrize(
    ("container", "code"),
    [
        (FakeContainer(status_code=137, oom_killed=True), "STRATEGY_RUNNER_OOM"),
        (FakeContainer(status_code=1), "STRATEGY_RUNNER_FAILED"),
        (FakeContainer(stdout=b"not json"), "STRATEGY_RUNNER_OUTPUT_INVALID"),
        (
            FakeContainer(stdout=b"x" * (128 * 1024 + 1)),
            "STRATEGY_RUNNER_OUTPUT_TOO_LARGE",
        ),
    ],
)
def test_runner_failures_are_stable_and_cleanup_is_guaranteed(
    container: FakeContainer, code: str
) -> None:
    client, _ = _client(container)

    with pytest.raises(StrategyRunnerFailure) as error:
        client.run(_payload())

    assert error.value.code == code
    assert container.removed is True


def test_runner_rejects_test_period_fields_before_creating_container() -> None:
    container = FakeContainer()
    client, docker_client = _client(container)
    payload = _payload()
    payload["context"] = {**payload["context"], "test_start_date": "2026-01-01"}

    with pytest.raises(StrategyRunnerFailure) as error:
        client.run(payload)

    assert error.value.code == "TEST_DATA_EXPOSED_TO_STRATEGY"
    assert docker_client.containers.create_kwargs is None


def test_runner_does_not_guess_leakage_from_nested_parameter_names() -> None:
    container = FakeContainer()
    client, _ = _client(container)
    payload = _payload()
    payload["parameters"] = {"test_window": 20}

    result = client.run(payload)

    assert result["low_strong"] == "1"


def test_runner_removes_container_when_input_copy_fails() -> None:
    container = FakeContainer(archive_error=RuntimeError("copy failed"))
    client, _ = _client(container)

    with pytest.raises(StrategyRunnerFailure) as error:
        client.run(_payload())

    assert error.value.code == "STRATEGY_RUNNER_FAILED"
    assert container.removed is True


def test_runner_requires_digest_pinned_image() -> None:
    with pytest.raises(ValueError, match="digest"):
        DockerStrategyRunnerClient(
            docker_client=FakeDockerClient(FakeContainer()),
            image="long-invest-strategy-runner:latest",
            seccomp_profile="/etc/long-invest/seccomp.json",
        )


def test_runner_records_failed_cleanup_and_can_recover_it() -> None:
    container = FakeContainer(remove_error=TimeoutError("docker unavailable"))
    client, _ = _client(container)

    client.run(_payload())

    assert client.pending_cleanup_container_ids == (container.id,)
    assert client.recover_pending_cleanup() == ()
    assert client.pending_cleanup_container_ids == ()
    assert container.removed is True
