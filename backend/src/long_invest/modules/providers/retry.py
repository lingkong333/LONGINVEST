from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx


class ProviderHttpError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def _retryable(error: Exception) -> bool:
    return isinstance(
        error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)
    ) or (isinstance(error, ProviderHttpError) and error.retryable)


async def run_with_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    deadline: datetime,
    attempts: int = 3,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    if deadline.tzinfo is None:
        raise ValueError("deadline must include timezone")
    for attempt in range(1, attempts + 1):
        if datetime.now(UTC) >= deadline:
            raise ProviderHttpError("PROVIDER_TIMEOUT")
        try:
            return await operation()
        except Exception as error:
            if attempt == attempts or not _retryable(error):
                raise
            remaining = (deadline - datetime.now(UTC)).total_seconds()
            delay = min(0.1 * 2 ** (attempt - 1), max(0.0, remaining))
            if delay <= 0:
                raise ProviderHttpError("PROVIDER_TIMEOUT") from error
            await sleep(delay)
    raise AssertionError("unreachable")
