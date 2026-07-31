import asyncio
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from long_invest.modules.providers.contracts import ProviderCapability, ProviderCode
from long_invest.modules.providers.models import (
    ProviderCircuitHistory,
    ProviderCircuitState,
)
from long_invest.modules.providers.postgres_runtime import (
    PostgresProviderRuntimeState,
)
from long_invest.modules.providers.resilience import ProviderRouteSetting
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database

BACKEND = Path(__file__).parents[2]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.skipif(
    os.getenv("LONGINVEST_PROVIDER_RUNTIME_TESTS") != "1",
    reason="set LONGINVEST_PROVIDER_RUNTIME_TESTS=1 for PostgreSQL runtime tests",
)
@pytest.mark.anyio
async def test_circuit_survives_restart_and_allows_one_recovery_probe() -> None:
    settings = AppSettings(_env_file=None)
    database_name = f"longinvest_provider_runtime_{uuid4().hex}"
    owner_base = make_url(settings.database_owner_url)
    app_base = make_url(settings.database_url)
    owner_url = owner_base.set(database=database_name)
    app_url = app_base.set(database=database_name)
    maintenance = create_async_engine(
        owner_base.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    environment = os.environ.copy()
    rendered_owner_url = owner_url.render_as_string(hide_password=False)
    rendered_app_url = app_url.render_as_string(hide_password=False)
    environment["LONGINVEST_DATABASE_OWNER_URL"] = rendered_owner_url
    environment["LONGINVEST_DATABASE_URL"] = rendered_app_url

    async with temporary_database(maintenance, database_name):
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=BACKEND,
            env=environment,
            check=True,
            text=True,
            timeout=180,
        )
        database = Database(rendered_app_url)
        setting = ProviderRouteSetting(
            ProviderCode.EASTMONEY,
            ProviderCapability.REALTIME_QUOTE_BATCH,
        )
        runtime = PostgresProviderRuntimeState(database)
        try:
            assert await runtime.allow(setting)
            for _ in range(3):
                await runtime.record_failure(setting)
            assert not await runtime.allow(setting)

            restarted = PostgresProviderRuntimeState(database)
            assert not await restarted.allow(setting)
            async with database.transaction() as session:
                await session.execute(
                    update(ProviderCircuitState).values(
                        opened_at=datetime.now(UTC) - timedelta(seconds=61)
                    )
                )
            grants = await asyncio.gather(
                *(restarted.allow(setting) for _ in range(4))
            )
            assert grants.count(True) == 1
            snapshot = await restarted.circuit_snapshot(setting)
            assert snapshot["state"] == "HALF_OPEN"
            assert snapshot["probe_token"] is not None

            await restarted.record_failure(setting)
            snapshot = await restarted.circuit_snapshot(setting)
            assert snapshot["state"] == "OPEN"
            assert snapshot["cooldown_index"] == 1

            await restarted.force_half_open(setting)
            assert await restarted.allow(setting)
            await restarted.record_success(setting)
            snapshot = await restarted.circuit_snapshot(setting)
            assert snapshot["state"] == "CLOSED"
            assert snapshot["consecutive_failures"] == 0
            assert snapshot["cooldown_index"] == 0
            assert snapshot["opened_at"] is None
            async with database.session() as session:
                transitions = list(
                    await session.scalars(
                        select(ProviderCircuitHistory).order_by(
                            ProviderCircuitHistory.occurred_at
                        )
                    )
                )
            assert [(item.from_state, item.to_state) for item in transitions] == [
                ("CLOSED", "OPEN"),
                ("OPEN", "HALF_OPEN"),
                ("HALF_OPEN", "OPEN"),
                ("OPEN", "HALF_OPEN"),
                ("HALF_OPEN", "CLOSED"),
            ]
        finally:
            await database.dispose()


@asynccontextmanager
async def temporary_database(maintenance, database_name: str):
    try:
        async with maintenance.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        yield
    finally:
        async with maintenance.connect() as connection:
            await connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        await maintenance.dispose()
