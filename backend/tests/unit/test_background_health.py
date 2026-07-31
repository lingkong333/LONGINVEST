import os
from pathlib import Path

from long_invest.entrypoints import background


def test_background_health_requires_a_fresh_heartbeat(
    monkeypatch, tmp_path: Path
) -> None:
    heartbeat = tmp_path / "background-heartbeat"
    monkeypatch.setattr(background, "BACKGROUND_HEARTBEAT_PATH", heartbeat)
    monkeypatch.setattr(background.time, "time", lambda: 100.0)

    assert background.is_healthy() is False

    heartbeat.touch()
    os.utime(heartbeat, (95.0, 95.0))
    assert background.is_healthy() is True

    os.utime(heartbeat, (60.0, 60.0))
    assert background.is_healthy() is False
