import asyncio
import re
import subprocess

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from long_invest.platform.config.settings import get_settings

ROLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$", re.ASCII)


async def ensure_application_role() -> None:
    settings = get_settings()
    if ROLE_PATTERN.fullmatch(settings.database_app_role) is None:
        raise ValueError("LONGINVEST_DATABASE_APP_ROLE is not a safe PostgreSQL role")

    engine = create_async_engine(settings.database_owner_url)
    try:
        async with engine.begin() as connection:
            exists = await connection.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"),
                {"role": settings.database_app_role},
            )
            operation = "ALTER" if exists else "CREATE"
            template = (
                f"{operation} ROLE %I WITH LOGIN PASSWORD %L"
                if exists
                else "CREATE ROLE %I WITH LOGIN PASSWORD %L"
            )
            statement = await connection.scalar(
                text("SELECT format(:template, :role, :password)"),
                {
                    "template": template,
                    "role": settings.database_app_role,
                    "password": settings.database_app_password,
                },
            )
            if not isinstance(statement, str):
                raise RuntimeError("failed to build PostgreSQL role statement")
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(ensure_application_role())
    subprocess.run(["alembic", "upgrade", "head"], check=True)


if __name__ == "__main__":
    main()
