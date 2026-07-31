import asyncio
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from long_invest.modules.auth.models import LoginRateLimitAttempt
from long_invest.modules.auth.postgres_rate_limit import PostgresLoginRateLimiter
from long_invest.modules.auth.rate_limit import RateLimitConfig
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database

BACKEND = Path(__file__).parents[2]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.skipif(
    os.getenv("LONGINVEST_AUTH_RATE_LIMIT_POSTGRES_TESTS") != "1",
    reason="requires a temporary migrated PostgreSQL database",
)
@pytest.mark.anyio
async def test_login_limit_is_atomic_persistent_private_and_recovers() -> None:
    settings = AppSettings(_env_file=None)
    database_name = f"longinvest_auth_limit_{uuid4().hex}"
    owner_base = make_url(settings.database_owner_url)
    app_base = make_url(settings.database_url)
    owner_url = owner_base.set(database=database_name)
    app_url = app_base.set(database=database_name)
    maintenance = create_async_engine(
        owner_base.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )
    environment = os.environ.copy()
    environment["LONGINVEST_DATABASE_OWNER_URL"] = owner_url.render_as_string(
        hide_password=False
    )
    environment["LONGINVEST_DATABASE_URL"] = app_url.render_as_string(
        hide_password=False
    )

    async with temporary_database(maintenance, database_name):
        subprocess.run(
            [sys.executable, "-m", "long_invest.entrypoints.migrate"],
            cwd=BACKEND,
            env=environment,
            check=True,
            text=True,
            timeout=180,
        )
        database = Database(app_url.render_as_string(hide_password=False))
        config = RateLimitConfig(
            per_ip=1,
            per_username=1,
            global_failures=2,
            window=timedelta(minutes=1),
        )
        limiter = PostgresLoginRateLimiter(database, config)
        ip = "203.0.113.9"
        username = "private-admin"
        now = datetime.now(UTC)
        try:
            first, second = await asyncio.gather(
                limiter.check(ip=ip, username=username, now=now),
                limiter.check(ip=ip, username=username, now=now),
            )
            assert [first.allowed, second.allowed].count(True) == 1
            assert [first.allowed, second.allowed].count(False) == 1

            allowed = first if first.allowed else second
            await limiter.record_success(
                ip=ip,
                username=username,
                now=now,
                reservation_id=allowed.reservation_id,
            )
            failure = await limiter.check(ip=ip, username=username, now=now)
            assert failure.allowed is True
            await limiter.record_failure(
                ip=ip,
                username=username,
                now=now,
                reservation_id=failure.reservation_id,
            )

            restarted = PostgresLoginRateLimiter(database, config)
            blocked = await restarted.check(ip=ip, username=username, now=now)
            recovered = await restarted.check(
                ip=ip,
                username=username,
                now=now + timedelta(minutes=1),
            )
            assert blocked.allowed is False
            assert blocked.retry_after_seconds == 60
            assert recovered.allowed is True

            async with database.session() as session:
                rows = list(await session.scalars(select(LoginRateLimitAttempt)))
            assert rows
            assert ip not in repr(rows)
            assert username not in repr(rows)
            assert all(len(row.ip_digest) == 64 for row in rows)
            assert all(len(row.username_digest) == 64 for row in rows)
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
