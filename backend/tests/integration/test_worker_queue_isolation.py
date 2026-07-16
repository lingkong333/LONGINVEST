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
    }
    for service_name, queue_name in expected.items():
        service = services[service_name]
        assert service["environment"]["LONGINVEST_WORKER_QUEUES"] == queue_name
        assert service["command"] == [
            "python",
            "-m",
            "long_invest.entrypoints.worker",
        ]
