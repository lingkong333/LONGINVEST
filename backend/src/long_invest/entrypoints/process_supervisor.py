import argparse
import asyncio
import json
import os
import signal
import sys
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import structlog

from long_invest.platform.config.settings import get_settings
from long_invest.platform.logging.configure import configure_logging

HEARTBEAT_SECONDS = 5
HEALTH_MAX_AGE_SECONDS = 30
RESTART_WINDOW_SECONDS = 60
MAX_RESTARTS_IN_WINDOW = 5
STOP_GRACE_SECONDS = 15
logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    name: str
    module: str
    environment: tuple[tuple[str, str], ...] = ()


def _worker(name: str, queue: str) -> ProcessSpec:
    return ProcessSpec(
        name=name,
        module="long_invest.entrypoints.worker",
        environment=(("LONGINVEST_WORKER_QUEUES", queue),),
    )


PROCESS_GROUPS: dict[str, tuple[ProcessSpec, ...]] = {
    "core": (
        ProcessSpec("dispatcher", "long_invest.entrypoints.dispatcher"),
        ProcessSpec("watchdog", "long_invest.entrypoints.watchdog"),
        ProcessSpec("background", "long_invest.entrypoints.background"),
        _worker("worker-maintenance", "maintenance"),
        _worker("worker-qfq-refresh", "qfq-refresh"),
        ProcessSpec("signal-projector", "long_invest.entrypoints.signal_projector"),
        ProcessSpec(
            "notification-wecom",
            "long_invest.entrypoints.notification_worker",
            (("LONGINVEST_NOTIFICATION_CHANNEL", "WECOM"),),
        ),
        ProcessSpec(
            "notification-email",
            "long_invest.entrypoints.notification_worker",
            (("LONGINVEST_NOTIFICATION_CHANNEL", "EMAIL"),),
        ),
        _worker("worker-signals", "signals"),
    ),
    "strategy": (
        _worker("worker-strategy", "strategy"),
        _worker("worker-strategy-targets", "strategy-targets"),
        _worker("worker-backtest-single", "backtest-single"),
        ProcessSpec(
            "worker-backtest-bulk",
            "long_invest.entrypoints.bulk_backtest_worker",
        ),
    ),
}


def process_specs(group_name: str) -> tuple[ProcessSpec, ...]:
    try:
        return PROCESS_GROUPS[group_name]
    except KeyError as exc:
        raise ValueError(f"unknown process group: {group_name}") from exc


def _registry_path(group_name: str) -> Path:
    return Path(f"/tmp/longinvest-supervisor-{group_name}.json")


def _child_environment(spec: ProcessSpec) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(spec.environment)
    environment["LONGINVEST_LOG_FILE"] = (
        f"/var/log/longinvest/{spec.name}.jsonl"
    )
    return environment


def _write_registry(group_name: str, active: dict[str, int]) -> None:
    target = _registry_path(group_name)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "group": group_name,
                "updated_at": time.time(),
                "processes": active,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    temporary.replace(target)


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=STOP_GRACE_SECONDS)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        await process.wait()


async def _supervise_process(
    spec: ProcessSpec,
    *,
    stop: asyncio.Event,
    active: dict[str, int],
) -> None:
    restart_times: deque[float] = deque()
    process: asyncio.subprocess.Process | None = None
    try:
        while not stop.is_set():
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                spec.module,
                env=_child_environment(spec),
                start_new_session=True,
            )
            active[spec.name] = process.pid
            logger.info(
                "supervised_process_started",
                category="application",
                process_name=spec.name,
                pid=process.pid,
            )
            process_wait = asyncio.create_task(process.wait())
            stop_wait = asyncio.create_task(stop.wait())
            done, pending = await asyncio.wait(
                {process_wait, stop_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if stop_wait in done:
                await _stop_process(process)
                return

            active.pop(spec.name, None)
            now = time.monotonic()
            restart_times.append(now)
            while restart_times and now - restart_times[0] > RESTART_WINDOW_SECONDS:
                restart_times.popleft()
            logger.error(
                "supervised_process_exited",
                category="application",
                process_name=spec.name,
                return_code=process.returncode,
                restarts_in_window=len(restart_times),
            )
            if len(restart_times) >= MAX_RESTARTS_IN_WINDOW:
                raise RuntimeError(f"process repeatedly failed: {spec.name}")
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop.wait(), timeout=min(2 ** len(restart_times), 10)
                )
    finally:
        active.pop(spec.name, None)
        if process is not None:
            await _stop_process(process)


async def _heartbeat(
    group_name: str,
    *,
    stop: asyncio.Event,
    active: dict[str, int],
) -> None:
    while not stop.is_set():
        _write_registry(group_name, active)
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SECONDS)
    _write_registry(group_name, active)


async def run(group_name: str) -> None:
    specs = process_specs(group_name)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(event, stop.set)
    active: dict[str, int] = {}
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(_heartbeat(group_name, stop=stop, active=active))
            for spec in specs:
                tasks.create_task(
                    _supervise_process(spec, stop=stop, active=active)
                )
    finally:
        _registry_path(group_name).unlink(missing_ok=True)


def is_healthy(group_name: str) -> bool:
    specs = process_specs(group_name)
    try:
        state = json.loads(_registry_path(group_name).read_text(encoding="utf-8"))
        if time.time() - float(state["updated_at"]) > HEALTH_MAX_AGE_SECONDS:
            return False
        processes = state["processes"]
        if set(processes) != {spec.name for spec in specs}:
            return False
        for pid in processes.values():
            os.kill(int(pid), 0)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("group", choices=tuple(PROCESS_GROUPS))
    parser.add_argument("--healthcheck", action="store_true")
    arguments = parser.parse_args()
    if arguments.healthcheck:
        raise SystemExit(0 if is_healthy(arguments.group) else 1)

    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        queue_capacity=settings.log_queue_capacity,
        log_file=settings.log_file,
        service=f"longinvest-supervisor-{arguments.group}",
    )
    asyncio.run(run(arguments.group))


if __name__ == "__main__":
    main()
