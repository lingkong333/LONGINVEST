import asyncio
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from long_invest.modules.providers.budget import ProviderRequestBudget
from long_invest.modules.providers.contracts import ProviderCapability, ProviderCode
from long_invest.modules.providers.models import (
    ProviderBudgetPolicy,
    ProviderBudgetUsage,
    ProviderCapabilitySetting,
    ProviderConfigVersion,
    ProviderRequestLease,
)
from long_invest.modules.providers.resilience import ProviderRouteSetting
from long_invest.modules.providers.retry import ProviderHttpError
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database

BACKEND = Path(__file__).parents[2]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.skipif(
    os.getenv("LONGINVEST_PROVIDER_BUDGET_TESTS") != "1",
    reason="set LONGINVEST_PROVIDER_BUDGET_TESTS=1 for PostgreSQL budget tests",
)
@pytest.mark.anyio
async def test_budget_survives_concurrency_restart_exhaustion_and_reset() -> None:
    settings = AppSettings(_env_file=None)
    database_name = f"longinvest_budget_{uuid4().hex}"
    owner_base = make_url(settings.database_owner_url)
    owner_url = owner_base.set(database=database_name)
    maintenance = create_async_engine(
        owner_base.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )
    environment = os.environ.copy()
    rendered_url = owner_url.render_as_string(hide_password=False)
    environment["LONGINVEST_DATABASE_OWNER_URL"] = rendered_url
    environment["LONGINVEST_DATABASE_URL"] = rendered_url

    async with temporary_database(maintenance, database_name):
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=BACKEND,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
        database = Database(rendered_url)
        await seed_policy(database)
        setting = ProviderRouteSetting(
            ProviderCode.EASTMONEY,
            ProviderCapability.REALTIME_QUOTE_BATCH,
            concurrency=2,
            rate_per_second=100,
            timeout_seconds=5,
        )
        budget = ProviderRequestBudget(database)
        try:
            results = await asyncio.gather(
                *(claim_or_code(budget, setting) for _ in range(6))
            )
            leases = [item for item in results if not isinstance(item, str)]
            denied = [item for item in results if isinstance(item, str)]
            assert len(leases) == 2
            assert denied == ["PROVIDER_TOTAL_CONCURRENCY_LIMITED"] * 4
            await asyncio.gather(*(budget.release(item) for item in leases))

            restarted_database = Database(rendered_url)
            restarted = ProviderRequestBudget(restarted_database)
            snapshot = await restarted.snapshot(ProviderCode.EASTMONEY)
            assert snapshot["used"] == 2
            await restarted_database.dispose()

            async with database.transaction() as session:
                await session.execute(delete(ProviderRequestLease))
                await session.execute(
                    update(ProviderBudgetUsage).values(
                        used_count=49_999,
                        last_request_at=None,
                        latest_limit_reason=None,
                        latest_limited_at=None,
                    )
                )
            last = await budget.claim(setting)
            await budget.release(last)
            with pytest.raises(ProviderHttpError) as captured:
                await budget.claim(setting)
            assert captured.value.code == "PROVIDER_DAILY_BUDGET_EXHAUSTED"
            snapshot = await budget.snapshot(ProviderCode.EASTMONEY)
            assert snapshot["used"] == 50_000
            assert snapshot["remaining"] == 0
            assert snapshot["latest_limit_reason"] == captured.value.code

            today = datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai")).date()
            async with database.transaction() as session:
                await session.execute(delete(ProviderRequestLease))
                await session.execute(delete(ProviderBudgetUsage))
                session.add(
                    ProviderBudgetUsage(
                        provider_code=ProviderCode.EASTMONEY.value,
                        capability=ProviderCapability.REALTIME_QUOTE_BATCH.value,
                        budget_date=today - timedelta(days=1),
                        used_count=50_000,
                    )
                )
            fresh = await budget.claim(setting)
            await budget.release(fresh)
            reset_snapshot = await budget.snapshot(ProviderCode.EASTMONEY)
            assert reset_snapshot["used"] == 1
            assert reset_snapshot["remaining"] == 49_999
            assert reset_snapshot["reset_at"] > datetime.now(UTC)
        finally:
            await database.dispose()


async def seed_policy(database: Database) -> None:
    async with database.transaction() as session:
        version = (
            int(
                await session.scalar(
                    select(
                        func.coalesce(func.max(ProviderConfigVersion.version), 0)
                    ).where(
                        ProviderConfigVersion.provider_code
                        == ProviderCode.EASTMONEY.value
                    )
                )
                or 0
            )
            + 1
        )
        session.add(
            ProviderConfigVersion(
                version=version,
                provider_code=ProviderCode.EASTMONEY.value,
                reason="provider budget integration test",
            )
        )
        session.add(
            ProviderBudgetPolicy(
                config_version=version,
                provider_code=ProviderCode.EASTMONEY.value,
                daily_limit=50_000,
                reset_timezone="Asia/Shanghai",
                max_concurrency=2,
                realtime_reserved=0,
                daily_reserved=0,
            )
        )
        session.add(
            ProviderCapabilitySetting(
                config_version=version,
                provider_code=ProviderCode.EASTMONEY.value,
                capability=ProviderCapability.REALTIME_QUOTE_BATCH.value,
                enabled=True,
                priority=1,
                concurrency=2,
                rate_per_second=100,
                timeout_seconds=5,
                auto_switch=True,
                daily_limit=50_000,
                min_interval_seconds=0,
            )
        )


async def claim_or_code(budget: ProviderRequestBudget, setting: ProviderRouteSetting):
    try:
        return await budget.claim(setting)
    except ProviderHttpError as error:
        return error.code


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
