import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from long_invest.platform.config.settings import AppSettings

BACKEND = Path(__file__).parents[2]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.skipif(
    os.getenv("LONGINVEST_LEGACY_RQ_MIGRATION_TESTS") != "1",
    reason="set LONGINVEST_LEGACY_RQ_MIGRATION_TESTS=1 for migration tests",
)
@pytest.mark.anyio
async def test_retirement_is_scoped_traceable_and_not_reversed() -> None:
    settings = AppSettings(_env_file=None)
    database_name = f"longinvest_rq_retirement_{uuid4().hex}"
    owner_base = make_url(settings.database_owner_url)
    owner_url = owner_base.set(database=database_name)
    maintenance = create_async_engine(
        owner_base.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    environment = os.environ.copy()
    rendered_owner_url = owner_url.render_as_string(hide_password=False)
    environment["LONGINVEST_DATABASE_OWNER_URL"] = rendered_owner_url
    environment["LONGINVEST_DATABASE_URL"] = rendered_owner_url

    async with temporary_database(maintenance, database_name):
        migrate(environment, "20260731_0036")
        engine = create_async_engine(rendered_owner_url)
        retired_id = uuid4()
        already_canceled_id = uuid4()
        retained_id = uuid4()
        outbox_id = uuid4()
        old_outbox_id = uuid4()
        async with engine.begin() as connection:
            for job_id, job_type in (
                (retired_id, "ADMIN_TEST"),
                (retained_id, "DAILY_MARKET_DATA"),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO job ("
                        "id, job_type, queue, module_owner, priority, status, "
                        "idempotency_scope, idempotency_key, request_hash, request_id"
                        ") VALUES ("
                        ":id, :job_type, 'maintenance', 'legacy', 0, 'QUEUED', "
                        ":scope, :key, :hash, :request_id)"
                    ),
                    {
                        "id": job_id,
                        "job_type": job_type,
                        "scope": f"test:{job_id}",
                        "key": str(job_id),
                        "hash": "0" * 64,
                        "request_id": f"request-{job_id}",
                    },
                )
            await connection.execute(
                text(
                    "INSERT INTO event_outbox ("
                    "id, topic, aggregate_type, aggregate_id, queue, payload, "
                    "dedupe_key, status) VALUES ("
                    ":id, 'jobs.control', 'job', :aggregate_id, 'maintenance', "
                    "'{}'::jsonb, :dedupe_key, 'PENDING')"
                ),
                {
                    "id": outbox_id,
                    "aggregate_id": str(retired_id),
                    "dedupe_key": f"legacy-control:{retired_id}",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO job ("
                    "id, job_type, queue, module_owner, priority, status, "
                    "idempotency_scope, idempotency_key, request_hash, request_id"
                    ") VALUES ("
                    ":id, 'ADMIN_TEST', 'maintenance', 'maintenance', 0, "
                    "'CANCELED', :scope, :key, :hash, :request_id)"
                ),
                {
                    "id": already_canceled_id,
                    "scope": f"test:{already_canceled_id}",
                    "key": str(already_canceled_id),
                    "hash": "1" * 64,
                    "request_id": f"request-{already_canceled_id}",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO event_outbox ("
                    "id, topic, aggregate_type, aggregate_id, queue, payload, "
                    "dedupe_key, status) VALUES ("
                    ":id, 'jobs.control', 'job', :aggregate_id, 'maintenance', "
                    "'{}'::jsonb, :dedupe_key, 'PENDING')"
                ),
                {
                    "id": old_outbox_id,
                    "aggregate_id": str(already_canceled_id),
                    "dedupe_key": f"old-control:{already_canceled_id}",
                },
            )

        migrate(environment, "head")
        async with engine.connect() as connection:
            retired = (
                await connection.execute(
                    text(
                        "SELECT status, last_error_code, result_summary, version "
                        "FROM job WHERE id = :id"
                    ),
                    {"id": retired_id},
                )
            ).one()
            retained_status = await connection.scalar(
                text("SELECT status FROM job WHERE id = :id"), {"id": retained_id}
            )
            outbox_status = await connection.scalar(
                text("SELECT status FROM event_outbox WHERE id = :id"),
                {"id": outbox_id},
            )
            old_outbox_status = await connection.scalar(
                text("SELECT status FROM event_outbox WHERE id = :id"),
                {"id": old_outbox_id},
            )
        assert retired.status == "CANCELED"
        assert retired.last_error_code == "LEGACY_RQ_REMOVED"
        assert retired.result_summary == {
            "migration": "20260731_0037",
            "reason": "LEGACY_RQ_REMOVED",
            "previous_status": "QUEUED",
        }
        assert retained_status == "QUEUED"
        assert outbox_status == "DEAD"
        assert old_outbox_status == "DEAD"

        migrate(environment, "20260731_0036", command="downgrade")
        migrate(environment, "head")
        async with engine.connect() as connection:
            replayed = (
                await connection.execute(
                    text("SELECT status, version FROM job WHERE id = :id"),
                    {"id": retired_id},
                )
            ).one()
        assert replayed == ("CANCELED", retired.version)
        await engine.dispose()


def migrate(
    environment: dict[str, str],
    revision: str,
    *,
    command: str = "upgrade",
) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", command, revision],
        cwd=BACKEND,
        env=environment,
        check=True,
        text=True,
        timeout=180,
    )


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
