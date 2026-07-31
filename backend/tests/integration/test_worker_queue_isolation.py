from pathlib import Path

import yaml


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

    assert len(backend_services) == 17
    assert {service["image"] for service in backend_services.values()} == {
        "${LONGINVEST_BACKEND_IMAGE:-longinvest-backend:local}"
    }


def test_default_compose_has_four_persistent_containers() -> None:
    compose_path = Path(__file__).parents[3] / "deploy" / "compose.yaml"
    services = yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"]
    default_persistent = {
        name
        for name, service in services.items()
        if "profiles" not in service and name not in {"migrate"}
    }

    assert default_persistent == {
        "postgres",
        "api",
        "frontend",
        "background-core",
    }


def test_consolidated_background_container_has_strategy_runner_permission() -> None:
    compose_path = Path(__file__).parents[3] / "deploy" / "compose.yaml"
    services = yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"]
    core = services["background-core"]

    assert "background-strategy" not in services
    assert any("docker.sock" in volume for volume in core["volumes"])
    assert core["mem_limit"] == "1536m"
    assert core["command"] == [
        "python",
        "-m",
        "long_invest.entrypoints.background",
    ]
    healthcheck = core["healthcheck"]["test"][-1]
    assert "longinvest-background-heartbeat" in healthcheck
    assert "st_mtime <= 30" in healthcheck


def test_history_backfill_runs_inside_the_existing_core_background() -> None:
    compose_path = Path(__file__).parents[3] / "deploy" / "compose.yaml"
    dockerfile_path = (
        Path(__file__).parents[3] / "deploy" / "docker" / "backend.Dockerfile"
    )
    services = yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"]
    dockerfile = dockerfile_path.read_text(encoding="utf-8")

    assert "worker-bulk-history" not in services
    assert "FROM base AS collector-runtime" not in dockerfile
    assert "uv sync --frozen --no-dev --extra collector" in dockerfile
    assert services["background-core"]["command"][-1] == (
        "long_invest.entrypoints.background"
    )


def test_compose_publishes_only_the_frontend_on_public_port() -> None:
    compose_path = Path(__file__).parents[3] / "deploy" / "compose.yaml"
    services = yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"]

    assert services["frontend"]["ports"] == [
        "${LONGINVEST_FRONTEND_BIND:-15173:8080}"
    ]
    assert services["api"]["ports"] == ["127.0.0.1:18080:8000"]
    assert "ports" not in services["postgres"]
    assert "ports" not in services["redis"]


def test_frontend_proxy_refreshes_api_container_address() -> None:
    nginx_path = Path(__file__).parents[3] / "deploy" / "docker" / "nginx.conf"
    nginx_config = nginx_path.read_text(encoding="utf-8")

    assert "resolver 127.0.0.11 valid=10s ipv6=off;" in nginx_config
    assert "set $api_upstream api:8000;" in nginx_config
    assert "proxy_pass http://$api_upstream;" in nginx_config
    assert "proxy_pass http://api:8000;" not in nginx_config


def test_postgres_background_is_an_isolated_private_service() -> None:
    compose_path = Path(__file__).parents[3] / "deploy" / "compose.yaml"
    services = yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"]
    service = services["background-core"]
    assert service["command"][-1] == "long_invest.entrypoints.background"
    assert "ports" not in service
    assert service["read_only"] is True
    assert "no-new-privileges:true" in service["security_opt"]


def test_signal_projector_is_not_a_separate_default_process() -> None:
    compose_path = Path(__file__).parents[3] / "deploy" / "compose.yaml"
    services = yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"]
    default_commands = {
        tuple(service.get("command", ()))
        for service in services.values()
        if "profiles" not in service
    }
    assert (
        "python",
        "-m",
        "long_invest.entrypoints.signal_projector",
    ) not in default_commands
