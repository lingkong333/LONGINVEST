from pathlib import Path

import yaml


def test_compose_workers_listen_only_to_their_role_queue() -> None:
    compose_path = Path(__file__).parents[3] / "deploy" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = compose["services"]

    expected = {
        "worker-maintenance": "maintenance",
        "worker-realtime-quotes": "realtime-quotes",
        "worker-daily-market-data": "daily-market-data",
        "worker-qfq-refresh": "qfq-refresh",
        "worker-signals": "signals",
    }
    for service_name, queue_name in expected.items():
        service = services[service_name]
        assert service["environment"]["LONGINVEST_WORKER_QUEUES"] == queue_name
        assert service["command"] == [
            "python",
            "-m",
            "long_invest.entrypoints.worker",
        ]


def test_compose_publishes_only_the_frontend_on_public_port() -> None:
    compose_path = Path(__file__).parents[3] / "deploy" / "compose.yaml"
    services = yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"]

    assert services["frontend"]["ports"] == ["15173:8080"]
    assert services["api"]["ports"] == ["127.0.0.1:18080:8000"]
    assert "ports" not in services["postgres"]
    assert "ports" not in services["redis"]


def test_monitor_scheduler_is_an_isolated_private_service() -> None:
    compose_path = Path(__file__).parents[3] / "deploy" / "compose.yaml"
    service = yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"][
        "monitor-scheduler"
    ]

    assert service["command"] == [
        "python",
        "-m",
        "long_invest.entrypoints.monitor_scheduler",
    ]
    assert "ports" not in service
    assert service["read_only"] is True
    assert service["mem_limit"] == "128m"
    assert "no-new-privileges:true" in service["security_opt"]


def test_signal_projector_is_an_isolated_private_service() -> None:
    compose_path = Path(__file__).parents[3] / "deploy" / "compose.yaml"
    service = yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"][
        "signal-projector"
    ]

    assert service["command"] == [
        "python",
        "-m",
        "long_invest.entrypoints.signal_projector",
    ]
    assert "ports" not in service
    assert service["read_only"] is True
    assert service["mem_limit"] == "128m"
    assert "no-new-privileges:true" in service["security_opt"]
