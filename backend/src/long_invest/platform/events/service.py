import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Final

from sqlalchemy.exc import SQLAlchemyError

from long_invest.platform.errors import AppError
from long_invest.platform.events.contracts import TOPIC_RESOURCE_TYPES, EventSource

MAX_STREAM_SEQUENCE: Final = 9_223_372_036_854_775_807


class EventStreamService:
    def __init__(
        self,
        source: EventSource,
        *,
        batch_size: int = 100,
        poll_seconds: float = 1.0,
        heartbeat_seconds: float = 15.0,
        operation_timeout_seconds: float = 3.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source = source
        self._batch_size = batch_size
        self._poll_seconds = poll_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._operation_timeout_seconds = operation_timeout_seconds
        self._sleep = sleep
        self._monotonic = monotonic

    async def resolve_cursor(self, last_event_id: str | None) -> int:
        if last_event_id is None:
            try:
                async with asyncio.timeout(self._operation_timeout_seconds):
                    return await self._source.latest_sequence()
            except (SQLAlchemyError, TimeoutError) as exc:
                raise _backend_unavailable() from exc
        try:
            sequence = int(last_event_id, 10)
        except ValueError as exc:
            raise _invalid_cursor() from exc
        if (
            sequence < 1
            or sequence > MAX_STREAM_SEQUENCE
            or str(sequence) != last_event_id
        ):
            raise _invalid_cursor()
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                exists = await self._source.contains_sequence(sequence)
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc
        if not exists:
            raise AppError(
                code="EVENT_CURSOR_NOT_FOUND",
                message="事件恢复位置不存在，请重新加载页面",
                status_code=409,
            )
        return sequence

    async def stream(
        self,
        *,
        cursor: int,
        is_disconnected: Callable[[], Awaitable[bool]],
        validate_session: Callable[[], Awaitable[None]],
    ) -> AsyncIterator[str]:
        yield ": connected\n\n"
        last_heartbeat = self._monotonic()
        while not await is_disconnected():
            try:
                async with asyncio.timeout(self._operation_timeout_seconds):
                    events = await self._source.fetch_after(
                        cursor, limit=self._batch_size
                    )
            except (SQLAlchemyError, TimeoutError):
                return
            for event in events:
                cursor = event.sequence
                yield _serialize_event(event.sequence, event.topic, event.aggregate_id)
            now = self._monotonic()
            if now - last_heartbeat >= self._heartbeat_seconds:
                try:
                    async with asyncio.timeout(self._operation_timeout_seconds):
                        await validate_session()
                except (AppError, TimeoutError):
                    return
                yield ": heartbeat\n\n"
                last_heartbeat = now
            if len(events) < self._batch_size:
                await self._sleep(self._poll_seconds)


def _serialize_event(sequence: int, topic: str, aggregate_id: str) -> str:
    resource_type = TOPIC_RESOURCE_TYPES[topic]
    resource_id = "secrets" if topic == "secrets.changed.v1" else aggregate_id
    data = json.dumps(
        {
            "resource_type": resource_type,
            "resource_id": resource_id,
            "version": sequence,
            "topic": topic,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"id: {sequence}\nevent: resource.changed\ndata: {data}\n\n"


def _invalid_cursor() -> AppError:
    return AppError(
        code="EVENT_CURSOR_INVALID",
        message="Last-Event-ID 格式不正确",
        status_code=422,
    )


def _backend_unavailable() -> AppError:
    return AppError(
        code="EVENT_BACKEND_UNAVAILABLE",
        message="实时事件服务暂时不可用",
        status_code=503,
    )
