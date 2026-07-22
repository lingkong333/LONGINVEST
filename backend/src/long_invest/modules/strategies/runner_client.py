from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from requests.exceptions import ReadTimeout
from urllib3.exceptions import ReadTimeoutError

from long_invest.modules.strategies.forecast import CONTEXT_FIELDS, HISTORY_COLUMNS
from long_invest.modules.strategies.runner_execution import PAYLOAD_FIELDS

MAX_TIMEOUT_SECONDS = 10.0
MAX_OUTPUT_BYTES = 128 * 1024
MAX_SECCOMP_PROFILE_BYTES = 64 * 1024
TRUSTED_SECCOMP_PROFILE_PATH = Path("/etc/long-invest/seccomp.json")
_DIGEST_PINNED_IMAGE = re.compile(r"^(?:sha256:[0-9a-f]{64}|.+@sha256:[0-9a-f]{64})$")
_WORKER_ID = re.compile(r"^[A-Za-z0-9_.-]{1,63}$")
FORECAST_TIMEOUT = "STRATEGY_FORECAST_TIMEOUT"
RUNNER_FAILED = "STRATEGY_RUNNER_FAILED"
OUTPUT_INVALID = "STRATEGY_RUNNER_OUTPUT_INVALID"
TEST_DATA_EXPOSED = "TEST_DATA_EXPOSED_TO_STRATEGY"
_CLEANUP_RESERVE_SECONDS = 0.25
_CLEANUP_TIMEOUT_SECONDS = 1.0
_MANAGED_LABEL = "long-invest.strategy-runner"
_WORKER_LABEL = "long-invest.strategy-worker"


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
        worker_id: str,
        timeout_seconds: float = MAX_TIMEOUT_SECONDS,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
    ) -> None:
        if not _DIGEST_PINNED_IMAGE.fullmatch(image):
            raise ValueError("runner image must be pinned by digest")
        if not _WORKER_ID.fullmatch(worker_id):
            raise ValueError("runner worker id is invalid")
        if timeout_seconds <= 0:
            raise ValueError("runner timeout must be positive")
        if max_output_bytes <= 0 or max_output_bytes > MAX_OUTPUT_BYTES:
            raise ValueError("runner output limit is invalid")
        self._docker_client = docker_client
        self._image = image
        self._worker_id = worker_id
        self._seccomp_profile_json = _load_trusted_seccomp_profile()
        self._timeout_seconds = min(float(timeout_seconds), MAX_TIMEOUT_SECONDS)
        self._max_output_bytes = max_output_bytes
        self._pending_cleanup_ids: set[str] = set()
        self._api = getattr(docker_client, "api", None)
        if self._api is None or not hasattr(self._api, "timeout"):
            raise ValueError("docker client must expose a bounded request timeout")
        self._api.timeout = min(float(self._api.timeout), self._timeout_seconds)
        self.recover_pending_cleanup()

    @property
    def pending_cleanup_container_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._pending_cleanup_ids))

    def recover_pending_cleanup(self) -> tuple[str, ...]:
        deadline = self._new_cleanup_deadline()
        try:
            containers = self._call(
                lambda: self._docker_client.containers.list(
                    all=True,
                    filters={
                        "label": [
                            f"{_MANAGED_LABEL}=true",
                            f"{_WORKER_LABEL}={self._worker_id}",
                        ]
                    },
                ),
                deadline,
            )
        except Exception:
            return self.pending_cleanup_container_ids
        remaining_ids: set[str] = set()
        for container in containers:
            container_id = getattr(container, "id", None)
            try:
                self._call(lambda value=container: value.remove(force=True), deadline)
            except Exception:
                if isinstance(container_id, str) and container_id:
                    remaining_ids.add(container_id)
                continue
        self._pending_cleanup_ids = remaining_ids
        return self.pending_cleanup_container_ids

    def run(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        self.recover_pending_cleanup()
        _validate_payload(payload)
        encoded_payload = _encode_payload(payload)
        container = None
        attached_socket = None
        deadline = time.monotonic() + self._timeout_seconds
        try:
            container = self._call(
                lambda: self._docker_client.containers.create(
                    image=self._image,
                    detach=True,
                    stdin_open=True,
                    network_disabled=True,
                    network_mode="none",
                    read_only=True,
                    user="65532:65532",
                    cap_drop=["ALL"],
                    security_opt=[
                        "no-new-privileges:true",
                        f"seccomp={self._seccomp_profile_json}",
                    ],
                    labels={
                        _MANAGED_LABEL: "true",
                        _WORKER_LABEL: self._worker_id,
                    },
                    entrypoint=[
                        "python",
                        "-m",
                        "long_invest.modules.strategies.runner_execution",
                    ],
                    command=[],
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
                        "config": {
                            "max-size": "128k",
                            "max-file": "1",
                            "compress": "false",
                        },
                    },
                ),
                deadline,
            )
            attached_socket = self._call(
                lambda: container.attach_socket(params={"stdin": True, "stream": True}),
                deadline,
            )
            self._call(container.start, deadline)
            self._send_payload(attached_socket, encoded_payload, deadline)
            attached_socket.close()
            attached_socket = None
            try:
                wait_timeout = max(
                    0.001, self._remaining(deadline) - _CLEANUP_RESERVE_SECONDS
                )
                wait_result = self._call(
                    lambda: container.wait(timeout=wait_timeout), deadline
                )
            except Exception as exc:
                if not _is_timeout_exception(exc):
                    raise
                self._best_effort_kill(container, self._new_cleanup_deadline())
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
                self._best_effort_kill(container, self._new_cleanup_deadline())
            raise StrategyRunnerFailure(
                FORECAST_TIMEOUT, "strategy runner exceeded its deadline"
            ) from exc
        except Exception as exc:
            raise StrategyRunnerFailure(
                RUNNER_FAILED, "strategy runner could not be executed"
            ) from exc
        finally:
            if attached_socket is not None:
                with suppress(Exception):
                    attached_socket.close()
            if container is not None:
                self._best_effort_remove(container, self._new_cleanup_deadline())

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

    def _send_payload(
        self, attached_socket: Any, encoded_payload: bytes, deadline: float
    ) -> None:
        transport = getattr(attached_socket, "_sock", attached_socket)
        transport.settimeout(self._remaining(deadline))
        transport.sendall(encoded_payload)

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

    @staticmethod
    def _new_cleanup_deadline() -> float:
        return time.monotonic() + _CLEANUP_TIMEOUT_SECONDS


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


def _encode_payload(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _is_timeout_exception(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, (TimeoutError, ReadTimeout, ReadTimeoutError)):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _load_trusted_seccomp_profile() -> str:
    try:
        raw_profile = TRUSTED_SECCOMP_PROFILE_PATH.read_bytes()
    except OSError as exc:
        raise ValueError("trusted seccomp profile cannot be read") from exc
    if not raw_profile or len(raw_profile) > MAX_SECCOMP_PROFILE_BYTES:
        raise ValueError("trusted seccomp profile size is invalid")
    try:
        profile = json.loads(
            raw_profile,
            parse_constant=_raise_invalid_seccomp_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("trusted seccomp profile is invalid JSON") from exc
    if not isinstance(profile, dict):
        raise ValueError("trusted seccomp profile must be a JSON object")
    try:
        return json.dumps(
            profile,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("trusted seccomp profile is invalid") from exc


def _raise_invalid_seccomp_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
