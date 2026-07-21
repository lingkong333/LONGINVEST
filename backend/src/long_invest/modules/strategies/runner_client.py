from __future__ import annotations

import io
import json
import re
import tarfile
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from requests.exceptions import ReadTimeout

from long_invest.modules.strategies.forecast import CONTEXT_FIELDS
from long_invest.modules.strategies.runner_execution import PAYLOAD_FIELDS

MAX_TIMEOUT_SECONDS = 10.0
MAX_OUTPUT_BYTES = 128 * 1024
_DIGEST_PINNED_IMAGE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")


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

    def run(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        _validate_payload(payload)
        archive = _build_input_archive(payload)
        container = None
        try:
            container = self._docker_client.containers.create(
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
            )
            container.put_archive("/tmp", archive)
            container.start()
            try:
                wait_result = container.wait(timeout=self._timeout_seconds)
            except (TimeoutError, ReadTimeout) as exc:
                with suppress(Exception):
                    container.kill()
                raise StrategyRunnerFailure(
                    "STRATEGY_FORECAST_TIMEOUT", "strategy execution timed out"
                ) from exc

            container.reload()
            if bool(container.attrs.get("State", {}).get("OOMKilled")):
                raise StrategyRunnerFailure(
                    "STRATEGY_RUNNER_OOM", "strategy exceeded its memory limit"
                )
            status_code = int(wait_result.get("StatusCode", -1))
            stdout = container.logs(stdout=True, stderr=False)
            stderr = container.logs(stdout=False, stderr=True)
            if len(stdout) + len(stderr) > self._max_output_bytes:
                raise StrategyRunnerFailure(
                    "STRATEGY_RUNNER_OUTPUT_TOO_LARGE",
                    "strategy output exceeded its limit",
                )
            if status_code != 0:
                raise StrategyRunnerFailure(
                    "STRATEGY_RUNNER_FAILED", "strategy runner exited unsuccessfully"
                )
            try:
                result = json.loads(stdout)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise StrategyRunnerFailure(
                    "STRATEGY_RUNNER_OUTPUT_INVALID",
                    "strategy runner returned invalid output",
                ) from exc
            if not isinstance(result, Mapping):
                raise StrategyRunnerFailure(
                    "STRATEGY_RUNNER_OUTPUT_INVALID",
                    "strategy runner returned invalid output",
                )
            return result
        except StrategyRunnerFailure:
            raise
        except Exception as exc:
            raise StrategyRunnerFailure(
                "STRATEGY_RUNNER_FAILED", "strategy runner could not be executed"
            ) from exc
        finally:
            if container is not None:
                with suppress(Exception):
                    container.remove(force=True)


def _validate_payload(payload: Mapping[str, object]) -> None:
    if set(payload) != PAYLOAD_FIELDS:
        raise StrategyRunnerFailure(
            "TEST_DATA_EXPOSED_TO_STRATEGY", "runner payload contains forbidden fields"
        )
    context = payload.get("context")
    if not isinstance(context, Mapping) or set(context) != CONTEXT_FIELDS:
        raise StrategyRunnerFailure(
            "TEST_DATA_EXPOSED_TO_STRATEGY", "runner context contains forbidden fields"
        )
    if _contains_test_field(payload):
        raise StrategyRunnerFailure(
            "TEST_DATA_EXPOSED_TO_STRATEGY", "runner payload contains test-period data"
        )


def _contains_test_field(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            (isinstance(key, str) and (key == "test" or key.startswith("test_")))
            or _contains_test_field(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_test_field(item) for item in value)
    return False


def _build_input_archive(payload: Mapping[str, object]) -> bytes:
    encoded = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        info = tarfile.TarInfo("input.json")
        info.size = len(encoded)
        info.mode = 0o400
        info.mtime = 0
        archive.addfile(info, io.BytesIO(encoded))
    return stream.getvalue()
