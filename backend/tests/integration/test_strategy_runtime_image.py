import shutil
import subprocess


def test_strategy_runtime_image_installs_git_for_gitpython() -> None:
    executable = shutil.which("git")

    assert executable is not None
    result = subprocess.run(
        [executable, "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.stdout.startswith("git version ")
