import json

import pytest
from sqlalchemy.exc import SQLAlchemyError

from long_invest.platform.errors import AppError
from long_invest.platform.events.contracts import StoredResourceEvent
from long_invest.platform.events.service import EventStreamService


class FakeSource:
    def __init__(self, events: tuple[StoredResourceEvent, ...] = ()) -> None:
        self.events = events
        self.fail_latest = False
        self.timeout_latest = False
        self.fail_fetch = False

    async def latest_sequence(self) -> int:
        if self.fail_latest:
            raise SQLAlchemyError("database unavailable")
        if self.timeout_latest:
            raise TimeoutError
        return max((event.sequence for event in self.events), default=0)

    async def contains_sequence(self, sequence: int) -> bool:
        return any(event.sequence == sequence for event in self.events)

    async def fetch_after(
        self, sequence: int, *, limit: int
    ) -> tuple[StoredResourceEvent, ...]:
        if self.fail_fetch:
            raise SQLAlchemyError("database unavailable")
        matching = tuple(event for event in self.events if event.sequence > sequence)
        return matching[:limit]


async def _never_disconnected() -> bool:
    return False


async def _valid_session() -> None:
    return None


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.mark.anyio
async def test_resolve_cursor_starts_at_current_tail_without_replay() -> None:
    source = FakeSource((StoredResourceEvent(12, "jobs.dispatch", "job-12"),))

    assert await EventStreamService(source).resolve_cursor(None) == 12


@pytest.mark.anyio
async def test_resolve_cursor_accepts_existing_decimal_sequence() -> None:
    source = FakeSource((StoredResourceEvent(12, "jobs.dispatch", "job-12"),))

    assert await EventStreamService(source).resolve_cursor("12") == 12


@pytest.mark.anyio
@pytest.mark.parametrize("value", ["", "0", "01", "-1", "abc", "9223372036854775808"])
async def test_resolve_cursor_rejects_invalid_last_event_id(value: str) -> None:
    with pytest.raises(AppError) as captured:
        await EventStreamService(FakeSource()).resolve_cursor(value)

    assert captured.value.code == "EVENT_CURSOR_INVALID"
    assert captured.value.status_code == 422


@pytest.mark.anyio
async def test_resolve_cursor_rejects_missing_or_unsupported_event() -> None:
    with pytest.raises(AppError) as captured:
        await EventStreamService(FakeSource()).resolve_cursor("12")

    assert captured.value.code == "EVENT_CURSOR_NOT_FOUND"
    assert captured.value.status_code == 409


@pytest.mark.anyio
async def test_resolve_cursor_translates_database_failure() -> None:
    source = FakeSource()
    source.fail_latest = True

    with pytest.raises(AppError) as captured:
        await EventStreamService(source).resolve_cursor(None)

    assert captured.value.code == "EVENT_BACKEND_UNAVAILABLE"
    assert captured.value.status_code == 503


@pytest.mark.anyio
async def test_resolve_cursor_translates_database_timeout() -> None:
    source = FakeSource()
    source.timeout_latest = True

    with pytest.raises(AppError) as captured:
        await EventStreamService(source).resolve_cursor(None)

    assert captured.value.code == "EVENT_BACKEND_UNAVAILABLE"
    assert captured.value.status_code == 503


@pytest.mark.anyio
async def test_stream_emits_only_sanitized_resource_invalidation() -> None:
    source = FakeSource(
        (StoredResourceEvent(13, "settings.changed.v1", "notification.policy"),)
    )
    stream = EventStreamService(source).stream(
        cursor=12,
        is_disconnected=_never_disconnected,
        validate_session=_valid_session,
    )

    assert await anext(stream) == ": connected\n\n"
    item = await anext(stream)
    assert item.startswith("id: 13\nevent: resource.changed\ndata: ")
    payload = json.loads(item.split("data: ", maxsplit=1)[1])
    assert payload == {
        "resource_type": "settings",
        "resource_id": "notification.policy",
        "version": 13,
        "topic": "settings.changed.v1",
    }
    assert "payload" not in item
    await stream.aclose()


@pytest.mark.anyio
async def test_stream_redacts_secret_key_identifier() -> None:
    source = FakeSource(
        (StoredResourceEvent(14, "secrets.changed.v1", "smtp.password"),)
    )
    stream = EventStreamService(source).stream(
        cursor=13,
        is_disconnected=_never_disconnected,
        validate_session=_valid_session,
    )

    await anext(stream)
    item = await anext(stream)

    assert '"resource_id":"secrets"' in item
    assert "smtp.password" not in item
    await stream.aclose()


@pytest.mark.anyio
async def test_notification_request_invalidates_notification_queries() -> None:
    source = FakeSource(
        (StoredResourceEvent(15, "signal.notification_requested", "subscription-1"),)
    )
    stream = EventStreamService(source).stream(
        cursor=14,
        is_disconnected=_never_disconnected,
        validate_session=_valid_session,
    )

    await anext(stream)
    item = await anext(stream)

    assert '"resource_type":"notifications"' in item
    await stream.aclose()


@pytest.mark.anyio
async def test_stream_resumes_strictly_after_last_event_id() -> None:
    source = FakeSource(
        (
            StoredResourceEvent(20, "jobs.dispatch", "job-20"),
            StoredResourceEvent(21, "jobs.control", "job-21"),
        )
    )
    stream = EventStreamService(source).stream(
        cursor=20,
        is_disconnected=_never_disconnected,
        validate_session=_valid_session,
    )

    await anext(stream)
    item = await anext(stream)

    assert item.startswith("id: 21\n")
    assert "job-20" not in item
    await stream.aclose()


@pytest.mark.anyio
async def test_empty_stream_validates_session_before_heartbeat() -> None:
    times = iter((0.0, 16.0))
    validations = 0

    async def validate() -> None:
        nonlocal validations
        validations += 1

    service = EventStreamService(
        FakeSource(),
        heartbeat_seconds=15,
        sleep=_no_sleep,
        monotonic=lambda: next(times),
    )
    stream = service.stream(
        cursor=0,
        is_disconnected=_never_disconnected,
        validate_session=validate,
    )

    await anext(stream)
    assert await anext(stream) == ": heartbeat\n\n"
    assert validations == 1
    await stream.aclose()


@pytest.mark.anyio
async def test_revoked_session_closes_stream_without_heartbeat() -> None:
    times = iter((0.0, 16.0))

    async def revoked() -> None:
        raise AppError(
            code="AUTH_SESSION_INVALID",
            message="Session 已失效",
            status_code=401,
        )

    stream = EventStreamService(
        FakeSource(),
        sleep=_no_sleep,
        monotonic=lambda: next(times),
    ).stream(
        cursor=0,
        is_disconnected=_never_disconnected,
        validate_session=revoked,
    )

    await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.anyio
async def test_database_failure_closes_established_stream() -> None:
    source = FakeSource()
    source.fail_fetch = True
    stream = EventStreamService(source).stream(
        cursor=0,
        is_disconnected=_never_disconnected,
        validate_session=_valid_session,
    )

    await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.anyio
async def test_client_disconnect_stops_before_database_poll() -> None:
    source = FakeSource()

    async def disconnected() -> bool:
        return True

    stream = EventStreamService(source).stream(
        cursor=0,
        is_disconnected=disconnected,
        validate_session=_valid_session,
    )

    await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
