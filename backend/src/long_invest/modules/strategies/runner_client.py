from __future__ import annotations

import io
import json
import re
import tarfile
import time
from collections.abc import Mapping
from typing import Any

from requests.exceptions import ReadTimeout

from long_invest.modules.strategies.forecast import CONTEXT_FIELDS, HISTORY_COLUMNS
from long_invest.modules.strategies.runner_execution import PAYLOAD_FIELDS

MAX_TIMEOUT_SECONDS = 10.0
MAX_OUTPUT_BYTES = 128 * 1024
_DIGEST_PINNED_IMAGE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
FORECAST_TIMEOUT = "STRATEGY_FORECAST_TIMEOUT"
RUNNER_FAILED = "STRATEGY_RUNNER_FAILED"
OUTPUT_INVALID = "STRATEGY_RUNNER_OUTPUT_INVALID"
TEST_DATA_EXPOSED = "TEST_DATA_EXPOSED_TO_STRATEGY"
_CLEANUP_RESERVE_SECONDS = 0.25


class StrategyRunnerFailure(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DockerStrategyRunnerClient:
    def __init__(
        self,
        *,
        docker_client: Any,
        image: str,
        seccomp_profile: str,
        timeout_seconds: float = MAX_TIMEOUT_SECONDS,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
    ) -> None:
        if not _DIGEST_PINNED_IMAGE.fullmatch(image):
            raise ValueError("runner image must be pinned by digest")
        if not seccomp_profile:
            raise ValueError("runner seccomp profile is required")
        if timeout_seconds <= 0:
            raise ValueError("runner timeout must be positive")
        if max_output_bytes <= 0 or max_output_bytes > MAX_OUTPUT_BYTES:
            raise ValueError("runner output limit is invalid")
        self._docker_client = docker_client
        self._image = image
        self._seccomp_profile = seccomp_profile
        self._timeout_seconds = min(float(timeout_seconds), MAX_TIMEOUT_SECONDS)
        self._max_output_bytes = max_output_bytes
        self._pending_cleanup_ids: set[str] = set()
        self._api = getattr(docker_client, "api", None)
        if self._api is None or not hasattr(self._api, "timeout"):
            raise ValueError("docker client must expose a bounded request timeout")
        self._api.timeout = min(float(self._api.timeout), self._timeout_seconds)

    @property
    def pending_cleanup_container_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._pending_cleanup_ids))

    def recover_pending_cleanup(self) -> tuple[str, ...]:
        deadline = time.monotonic() + self._timeout_seconds
        for container_id in tuple(self._pending_cleanup_ids):
            try:
                container = self._call(
                    lambda value=container_id: (
                        self._docker_client.containers.get(value)
                    ),
                    deadline,
                )
                self._call(
                    lambda value=container: value.remove(force=True), deadline
                )
            except Exception:
                continue
            self._pending_cleanup_ids.discard(container_id)
        return self.pending_cleanup_container_ids

    def run(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        _validate_payload(payload)
        archive = _build_input_archive(payload)
        container = None
        deadline = time.monotonic() + self._timeout_seconds
        try:
            container = self._call(
                lambda: self._docker_client.containers.create(
                    image=self._image,
                    detach=True,
                    network_disabled=True,
                    network_mode="none",
                    read_only=True,
                    user="65532:65532",
                    cap_drop=["ALL"],
                    security_opt=[
                        "no-new-privileges:true",
                        f"seccomp={self._seccomp_profile}",
                    ],
                    nano_cpus=1_000_000_000,
                    mem_limit="512m",
                    memswap_limit="512m",
                    pids_limit=32,
                    tmpfs={"/tmp": "rw,noexec,nosuid,nodev,size=64m"},
                    volumes={},
                    environment={},
                    working_dir="/tmp",
                    init=True,
                    log_config={
                        "type": "local",
                        "config": {"max-size": "128k", "max-file": "1"},
                    },
                ),
                deadline,
            )
            self._call(container.start, deadline)
            self._call(lambda: container.put_archive("/tmp", archive), deadline)
            try:
                wait_timeout = max(
                    0.001, self._remaining(deadline) - _CLEANUP_RESERVE_SECONDS
                )
                wait_result = self._call(
                    lambda: container.wait(timeout=wait_timeout), deadline
                )
            except (TimeoutError, ReadTimeout) as exc:
                self._best_effort_kill(container, deadline)
                raise StrategyRunnerFailure(
                    FORECAST_TIMEOUT, "strategy execution timed out"
                ) from exc

            self._call(container.reload, deadline)
            if bool(container.attrs.get("State", {}).get("OOMKilled")):
                raise StrategyRunnerFailure(
                    "STRATEGY_RUNNER_OOM", "strategy exceeded its memory limit"
                )
            status_code = int(wait_result.get("StatusCode", -1))
            stdout = self._call(
                lambda: container.logs(stdout=True, stderr=False), deadline
            )
            stderr = self._call(
                lambda: container.logs(stdout=False, stderr=True), deadline
            )
            if len(stdout) + len(stderr) > self._max_output_bytes:
                raise StrategyRunnerFailure(
                    "STRATEGY_RUNNER_OUTPUT_TOO_LARGE",
                    "strategy output exceeded its limit",
                )
            if status_code != 0:
                raise StrategyRunnerFailure(
                    RUNNER_FAILED, "strategy runner exited unsuccessfully"
                )
            try:
                result = json.loads(stdout)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise StrategyRunnerFailure(
                    OUTPUT_INVALID,
                    "strategy runner returned invalid output",
                ) from exc
            if not isinstance(result, Mapping):
                raise StrategyRunnerFailure(
                    OUTPUT_INVALID,
                    "strategy runner returned invalid output",
                )
            return result
        except StrategyRunnerFailure:
            raise
        except (TimeoutError, ReadTimeout) as exc:
            if container is not None:
                self._best_effort_kill(container, deadline)
            raise StrategyRunnerFailure(
                FORECAST_TIMEOUT, "strategy runner exceeded its deadline"
            ) from exc
        except Exception as exc:
            raise StrategyRunnerFailure(
                RUNNER_FAILED, "strategy runner could not be executed"
            ) from exc
        finally:
            if container is not None:
                self._best_effort_remove(container, deadline)

    def _call(self, operation: Any, deadline: float) -> Any:
        remaining = self._remaining(deadline)
        self._api.timeout = min(remaining, MAX_TIMEOUT_SECONDS)
        return operation()

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("strategy runner deadline expired")
        return remaining

    def _best_effort_kill(self, container: Any, deadline: float) -> None:
        try:
            self._call(container.kill, deadline)
        except Exception:
            self._record_pending_cleanup(container)

    def _best_effort_remove(self, container: Any, deadline: float) -> None:
        try:
            self._call(lambda: container.remove(force=True), deadline)
        except Exception:
            self._record_pending_cleanup(container)
        else:
            container_id = getattr(container, "id", None)
            if isinstance(container_id, str):
                self._pending_cleanup_ids.discard(container_id)

    def _record_pending_cleanup(self, container: Any) -> None:
        container_id = getattr(container, "id", None)
        if isinstance(container_id, str) and container_id:
            self._pending_cleanup_ids.add(container_id)


def _validate_payload(payload: Mapping[str, object]) -> None:
    if set(payload) != PAYLOAD_FIELDS:
        raise StrategyRunnerFailure(
            TEST_DATA_EXPOSED, "runner payload contains forbidden fields"
        )
    source_code = payload.get("source_code")
    parameters = payload.get("parameters")
    context = payload.get("context")
    history = payload.get("history")
    if not isinstance(context, Mapping) or set(context) != CONTEXT_FIELDS:
        raise StrategyRunnerFailure(
            TEST_DATA_EXPOSED, "runner context contains forbidden fields"
        )
    if (
        not isinstance(source_code, str)
        or not isinstance(parameters, Mapping)
        or not isinstance(history, list)
        or any(
            not isinstance(row, Mapping) or set(row) != set(HISTORY_COLUMNS)
            for row in history
        )
    ):
        raise StrategyRunnerFailure(
            TEST_DATA_EXPOSED, "runner payload was not built from a training snapshot"
        )


def _build_input_archive(payload: Mapping[str, object]) -> bytes:
    encoded = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        info = tarfile.TarInfo("input.json")
        info.size = len(encoded)
        info.mode = 0o400
        info.uid = 65532
        info.gid = 65532
        info.uname = "nonroot"
        info.gname = "nonroot"
        info.mtime = 0
        archive.addfile(info, io.BytesIO(encoded))
    return stream.getvalue()
