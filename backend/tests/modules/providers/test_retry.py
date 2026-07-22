import asyncio
import ssl
from datetime import UTC, datetime, timedelta
from functools import wraps

import httpx
import pytest

from long_invest.modules.providers.retry import ProviderHttpError, run_with_retry


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


@async_test
@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("connect"),
        httpx.ReadTimeout("read"),
        httpx.RemoteProtocolError("peer closed response"),
        ProviderHttpError("PROVIDER_UPSTREAM_TEMPORARY", retryable=True),
    ],
)
async def test_retryable_failures_make_at_most_three_attempts(
    failure: Exception,
) -> None:
    attempts = 0

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise failure
        return "ok"

    result = await run_with_retry(
        operation,
        deadline=datetime.now(UTC) + timedelta(seconds=2),
        sleep=lambda _: _done(),
    )
    assert result == "ok"
    assert attempts == 3


@async_test
@pytest.mark.parametrize(
    "failure",
    [
        httpx.UnsupportedProtocol("tls"),
        ValueError("schema"),
        ProviderHttpError("PROVIDER_SCHEMA_INCOMPATIBLE"),
        ProviderHttpError("PROVIDER_RESPONSE_TOO_LARGE"),
        ProviderHttpError("PROVIDER_UNEXPECTED_CONTENT"),
    ],
)
async def test_permanent_failures_are_not_retried(failure: Exception) -> None:
    attempts = 0

    async def operation() -> None:
        nonlocal attempts
        attempts += 1
        raise failure

    with pytest.raises(type(failure)):
        await run_with_retry(
            operation, deadline=datetime.now(UTC) + timedelta(seconds=2)
        )
    assert attempts == 1


@async_test
async def test_tls_connect_failure_is_not_retried() -> None:
    attempts = 0
    failure = httpx.ConnectError("tls handshake")
    failure.__cause__ = ssl.SSLError("certificate verify failed")

    async def operation() -> None:
        nonlocal attempts
        attempts += 1
        raise failure

    with pytest.raises(httpx.ConnectError):
        await run_with_retry(
            operation,
            deadline=datetime.now(UTC) + timedelta(seconds=2),
        )
    assert attempts == 1


async def _done() -> None:
    return None
