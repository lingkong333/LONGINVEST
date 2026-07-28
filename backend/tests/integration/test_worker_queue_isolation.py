from pathlib import Path

import yaml

from long_invest.entrypoints.process_supervisor import process_specs


def test_compose_backend_runtime_services_share_one_image() -> None:
    compose_path = Path(__file__).parents[3] / "deploy" / "compose.yaml"
    services = yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"]
    backend_services = {
        name: service
        for name, service in services.items()
        if service.get("build", {}).get("dockerfile")
        == "deploy/docker/backend.Dockerfile"
        and service["build"].get("target") == "runtime"
    }

    assert len(backend_services) == 19
    assert {service["image"] for service in backend_services.values()} == {
        "${LONGINVEST_BACKEND_IMAGE:-longinvest-backend:local}"
    }


def test_compose_workers_listen_only_to_their_role_queue() -> None:
    expected = {
        "worker-maintenance": "maintenance",
        "worker-realtime-quotes": "realtime-quotes",
        "worker-daily-market-data": "daily-market-data",
        "worker-qfq-refresh": "qfq-refresh",
        "worker-signals": "signals",
    }
    actual = {
        spec.name: dict(spec.environment).get("LONGINVEST_WORKER_QUEUES")
        for spec in process_specs("core")
        if spec.name in expected
    }

    assert actual == expected


def test_default_compose_has_seven_persistent_containers() -> None:
    compose_path = Path(__file__).parents[3] / "deploy" / "compose.yaml"
    services = yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"]
    default_persistent = {
        name
        for name, service in services.items()
        if "profiles" not in service and name not in {"migrate"}
    }

    assert default_persistent == {
        "postgres",
        "redis",
        "api",
        "frontend",
        "background-core",
        "background-strategy",
        "worker-bulk-history",
    }


def test_consolidated_background_containers_keep_permission_boundary() -> None:
    compose_path = Path(__file__).parents[3] / "deploy" / "compose.yaml"
    services = yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"]
    core = services["background-core"]
    strategy = services["background-strategy"]

    assert not any("docker.sock" in volume for volume in core["volumes"])
    assert any("docker.sock" in volume for volume in strategy["volumes"])
    assert core["mem_limit"] == "768m"
    assert strategy["mem_limit"] == "1536m"
    assert core["healthcheck"]["test"][-1] == "core"
    assert strategy["healthcheck"]["test"][-1] == "strategy"


def test_bulk_history_worker_uses_isolated_browser_image() -> None:
    compose_path = Path(__file__).parents[3] / "deploy" / "compose.yaml"
    dockerfile_path = (
        Path(__file__).parents[3] / "deploy" / "docker" / "history-worker.Dockerfile"
    )
    service = yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"][
        "worker-bulk-history"
    ]
    dockerfile = dockerfile_path.read_text(encoding="utf-8")

    assert service["build"]["dockerfile"] == (
        "deploy/docker/history-worker.Dockerfile"
    )
    assert service["environment"]["LONGINVEST_WORKER_QUEUES"] == "bulk-history"
    assert (
        service["environment"]["LONGINVEST_EASTMONEY_HISTORY_TRANSPORT"]
        == "playwright"
    )
    assert service["read_only"] is True
    assert service["shm_size"] == "512m"
    assert service["mem_limit"] == "1g"
    assert "ALL" in service["cap_drop"]
    assert "ports" not in service
    assert "useradd --uid 999" in dockerfile
    assert "playwright install chrome" in dockerfile
    assert 'ENV HOME="/tmp"' in dockerfile
    assert "USER longinvest" in dockerfile


def test_compose_publishes_only_the_frontend_on_public_port() -> None:
    compose_path = Path(__file__).parents[3] / "deploy" / "compose.yaml"
    services = yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"]

    assert services["frontend"]["ports"] == [
        "${LONGINVEST_FRONTEND_BIND:-15173:8080}"
    ]
    assert services["api"]["ports"] == ["127.0.0.1:18080:8000"]
    assert "ports" not in services["postgres"]
    assert "ports" not in services["redis"]


def test_monitor_scheduler_is_an_isolated_private_service() -> None:
    compose_path = Path(__file__).parents[3] / "deploy" / "compose.yaml"
    services = yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"]
    service = services["background-core"]
    scheduler = next(
        spec for spec in process_specs("core") if spec.name == "monitor-scheduler"
    )

    assert scheduler.module == "long_invest.entrypoints.monitor_scheduler"
    assert "ports" not in service
    assert service["read_only"] is True
    assert "no-new-privileges:true" in service["security_opt"]


def test_signal_projector_is_an_isolated_private_service() -> None:
    compose_path = Path(__file__).parents[3] / "deploy" / "compose.yaml"
    services = yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"]
    service = services["background-core"]
    projector = next(
        spec for spec in process_specs("core") if spec.name == "signal-projector"
    )

    assert projector.module == "long_invest.entrypoints.signal_projector"
    assert "ports" not in service
    assert service["read_only"] is True
    assert "no-new-privileges:true" in service["security_opt"]
