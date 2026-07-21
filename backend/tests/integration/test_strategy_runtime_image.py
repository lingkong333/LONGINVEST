from pathlib import Path


DOCKERFILE = Path(__file__).parents[3] / "deploy" / "docker" / "backend.Dockerfile"


def test_strategy_runtime_image_installs_git_for_gitpython() -> None:
    source = DOCKERFILE.read_text(encoding="utf-8")

    assert "apt-get install --no-install-recommends --yes git" in source
