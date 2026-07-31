import argparse
import time

from long_invest.entrypoints.monitor_scheduler import (
    BACKGROUND_HEARTBEAT_PATH,
    run,
)
from long_invest.entrypoints.monitor_scheduler import (
    main as scheduler_main,
)

HEALTH_MAX_AGE_SECONDS = 30

__all__ = ["main", "run"]


def is_healthy() -> bool:
    try:
        return (
            time.time() - BACKGROUND_HEARTBEAT_PATH.stat().st_mtime
            <= HEALTH_MAX_AGE_SECONDS
        )
    except OSError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--healthcheck", action="store_true")
    arguments = parser.parse_args()
    if arguments.healthcheck:
        raise SystemExit(0 if is_healthy() else 1)
    BACKGROUND_HEARTBEAT_PATH.touch()
    try:
        scheduler_main(service="longinvest-background")
    finally:
        BACKGROUND_HEARTBEAT_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
