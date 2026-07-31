import json
from pathlib import Path

import pytest

from long_invest.entrypoints import process_supervisor


def test_process_groups_keep_queue_and_permission_boundaries() -> None:
    core = process_supervisor.process_specs("core")

    assert len(core) == 7
    assert {spec.name for spec in core} == {
        "dispatcher",
        "watchdog",
        "background",
        "worker-maintenance",
        "worker-qfq-refresh",
        "signal-projector",
        "worker-signals",
    }


def test_notification_channels_do_not_require_separate_processes() -> None:
    assert not any(
        spec.name.startswith("notification-")
        for spec in process_supervisor.process_specs("core")
    )


def test_unknown_process_group_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown process group"):
        process_supervisor.process_specs("missing")


def test_healthcheck_requires_fresh_complete_live_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = tmp_path / "registry.json"
    monkeypatch.setattr(
        process_supervisor,
        "_registry_path",
        lambda _group_name: registry,
    )
    monkeypatch.setattr(process_supervisor.time, "time", lambda: 100.0)
    monkeypatch.setattr(process_supervisor.os, "kill", lambda _pid, _signal: None)
    registry.write_text(
        json.dumps(
            {
                "updated_at": 95.0,
                "processes": {
                    spec.name: index + 1
                    for index, spec in enumerate(
                        process_supervisor.process_specs("core")
                    )
                },
            }
        ),
        encoding="utf-8",
    )

    assert process_supervisor.is_healthy("core") is True

    registry.write_text(
        json.dumps({"updated_at": 95.0, "processes": {}}), encoding="utf-8"
    )
    assert process_supervisor.is_healthy("core") is False
