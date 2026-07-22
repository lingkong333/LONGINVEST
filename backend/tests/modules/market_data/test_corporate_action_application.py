import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from long_invest.modules.market_data.application import (
    CorporateActionCollectionApplication,
)
from long_invest.modules.providers.contracts import (
    CorporateActionRecord,
    CorporateActionType,
    ProviderBatchResult,
    ProviderCode,
)
from long_invest.modules.providers.retry import ProviderHttpError


class Database:
    @asynccontextmanager
    async def transaction(self):
        yield object()


class Recorder:
    command = None

    async def record_fetch(self, command):
        self.command = command
        return command.batch_id


def test_collection_maps_verified_records_into_a_successful_batch() -> None:
    async def scenario() -> None:
        now = datetime(2026, 7, 22, tzinfo=UTC)
        record = CorporateActionRecord(
            symbol="600000.SH",
            source_event_id="AN1",
            event_type=CorporateActionType.COMPOSITE,
            event_date=date(2026, 7, 20),
            effective_date=date(2026, 7, 21),
            published_at=now - timedelta(days=2),
            observed_at=now - timedelta(seconds=1),
            adjustment_factor=Decimal("0.8"),
            source_reference="announcement:AN1",
            raw_payload_hash="a" * 64,
            source=ProviderCode.EASTMONEY,
        )
        recorder = Recorder()
        application = CorporateActionCollectionApplication(
            Database(),
            providers=SimpleNamespace(
                corporate_actions=_async_value(ProviderBatchResult((record,)))
            ),
            repository_factory=lambda session: session,
            service_factory=lambda repository: recorder,
            clock=lambda: now,
        )
        batch_id = uuid4()

        assert await _collect(application, batch_id) == batch_id
        assert recorder.command.succeeded is True
        assert recorder.command.error_code is None
        assert recorder.command.facts[0].source_event_id == "AN1"

    asyncio.run(scenario())


def test_collection_persists_provider_failure_without_partial_facts() -> None:
    async def scenario() -> None:
        async def fail(*_args, **_kwargs):
            raise ProviderHttpError("ADJUSTMENT_DATA_UNAVAILABLE")

        recorder = Recorder()
        application = CorporateActionCollectionApplication(
            Database(),
            providers=SimpleNamespace(corporate_actions=fail),
            repository_factory=lambda session: session,
            service_factory=lambda repository: recorder,
            clock=lambda: datetime(2026, 7, 22, tzinfo=UTC),
        )

        await _collect(application, uuid4())
        assert recorder.command.succeeded is False
        assert recorder.command.error_code == "ADJUSTMENT_DATA_UNAVAILABLE"
        assert recorder.command.facts == ()

    asyncio.run(scenario())


async def _collect(application, batch_id):
    return await application.collect(
        batch_id=batch_id,
        security_id=uuid4(),
        symbol="600000.SH",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        deadline=datetime(2026, 7, 22, tzinfo=UTC) + timedelta(seconds=10),
    )


def _async_value(value):
    async def resolve(*_args, **_kwargs):
        return value

    return resolve
