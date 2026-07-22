from functools import lru_cache

from long_invest.platform.database.engine import get_database
from long_invest.platform.events.repository import PostgresEventSource
from long_invest.platform.events.service import EventStreamService


@lru_cache
def get_event_stream_service() -> EventStreamService:
    return EventStreamService(PostgresEventSource(get_database()))
