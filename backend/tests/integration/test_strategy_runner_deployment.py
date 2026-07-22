import json
from pathlib import Path

RUNNER_DOCKERFILE = Path("/deploy/docker/strategy-runner.Dockerfile")
SECCOMP_PROFILE = Path("/deploy/security/strategy-runner-seccomp.json")


def test_runner_image_uses_pinned_non_root_entrypoint() -> None:
    source = RUNNER_DOCKERFILE.read_text(encoding="utf-8")

    assert "FROM python:3.12-slim@sha256:" in source
    assert "COPY --from=ghcr.io/astral-sh/uv:0.10.9@sha256:" in source
    assert "USER 65532:65532" in source
    assert "long_invest.modules.strategies.runner_execution" in source


def test_runner_seccomp_denies_network_and_host_control_syscalls() -> None:
    profile = json.loads(SECCOMP_PROFILE.read_text(encoding="utf-8"))
    denied = {
        name
        for group in profile["syscalls"]
        if group["action"] == "SCMP_ACT_ERRNO"
        for name in group["names"]
    }

    assert profile["defaultAction"] == "SCMP_ACT_ALLOW"
    assert {"socket", "connect", "mount", "ptrace", "bpf", "setns", "unshare"} <= denied
