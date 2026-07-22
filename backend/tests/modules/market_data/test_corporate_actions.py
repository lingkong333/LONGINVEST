import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from long_invest.modules.market_data.models import (
    CorporateActionFact,
    CorporateActionFetchBatch,
)
from long_invest.modules.market_data.service import (
    CorporateActionFactInput,
    CorporateActionService,
    RecordCorporateActionFetch,
)
from long_invest.platform.errors import AppError

OBSERVED = datetime(2026, 1, 5, 8, tzinfo=UTC)
SECURITY_ID = uuid4()


class MemoryCorporateActionRepository:
    def __init__(self) -> None:
        self.batches: dict[object, CorporateActionFetchBatch] = {}
        self.facts: list[CorporateActionFact] = []
        self.flush_calls = 0
        self.concurrent_batch: CorporateActionFetchBatch | None = None
        self.concurrent_fact: CorporateActionFact | None = None

    async def get_batch(self, batch_id):
        return self.batches.get(batch_id)

    async def list_event_facts_for_update(
        self, *, security_id, source, source_event_ids
    ):
        return [
            fact
            for fact in self.facts
            if fact.security_id == security_id
            and fact.source == source
            and fact.source_event_id in source_event_ids
        ]

    async def claim_fetch(self, batch, facts):
        self.flush_calls += 1
        if self.concurrent_batch is not None:
            concurrent = self.concurrent_batch
            self.batches[concurrent.id] = concurrent
            if self.concurrent_fact is not None:
                self.facts.append(self.concurrent_fact)
                self.concurrent_batch = None
                self.concurrent_fact = None
                return None, False
            return concurrent, False
        self.batches[batch.id] = batch
        self.facts.extend(facts)
        return batch, True

    async def list_covering_batches(self, *, security_id, start_date, end_date, as_of):
        rows = [
            batch
            for batch in self.batches.values()
            if batch.security_id == security_id
            and batch.status == "SUCCESS"
            and batch.coverage_start <= start_date
            and batch.coverage_end >= end_date
            and batch.observed_at <= as_of
            and batch.fetched_at <= as_of
        ]
        return sorted(
            rows,
            key=lambda batch: (
                batch.observed_at,
                batch.fetched_at,
                str(batch.id),
            ),
            reverse=True,
        )

    async def list_facts(
        self,
        *,
        security_id,
        source,
        start_date,
        end_date,
        as_of,
        observed_through,
    ):
        return [
            fact
            for fact in self.facts
            if (batch := self.batches[fact.batch_id]).security_id == security_id
            and batch.source == source
            and batch.status == "SUCCESS"
            and batch.observed_at <= observed_through
            and batch.fetched_at <= as_of
            and start_date <= fact.effective_date <= end_date
        ]


def fact(**overrides: object) -> CorporateActionFactInput:
    values = {
        "source_event_id": "event-1",
        "event_type": "DIVIDEND",
        "event_date": date(2026, 1, 9),
        "effective_date": date(2026, 1, 10),
        "published_at": datetime(2026, 1, 2, tzinfo=UTC),
        "observed_at": OBSERVED,
        "adjustment_factor": Decimal("0.9"),
        "source_reference": "provider:event-1",
        "raw_content_hash": "a" * 64,
    }
    values.update(overrides)
    return CorporateActionFactInput(**values)  # type: ignore[arg-type]


def command(**overrides: object) -> RecordCorporateActionFetch:
    values = {
        "batch_id": uuid4(),
        "security_id": SECURITY_ID,
        "source": "EASTMONEY",
        "provider_contract_version": "corporate-actions-v1",
        "coverage_start": date(2026, 1, 1),
        "coverage_end": date(2026, 1, 31),
        "observed_at": OBSERVED,
        "fetched_at": OBSERVED + timedelta(minutes=1),
        "succeeded": True,
        "facts": (),
    }
    values.update(overrides)
    return RecordCorporateActionFetch(**values)  # type: ignore[arg-type]


def query(service: CorporateActionService, *, as_of=None):
    return asyncio.run(
        service.get_adjustment_timeline(
            security_id=SECURITY_ID,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            as_of=as_of or OBSERVED + timedelta(hours=1),
        )
    )


def test_successful_empty_fetch_proves_empty_coverage() -> None:
    repository = MemoryCorporateActionRepository()
    service = CorporateActionService(repository)

    asyncio.run(service.record_fetch(command()))
    first = query(service)
    second = query(service)

    assert first.entries == ()
    assert first.row_count == 0
    assert first.snapshot_id == second.snapshot_id
    assert first.content_hash == second.content_hash


def test_latest_observable_batch_selects_latest_revision() -> None:
    repository = MemoryCorporateActionRepository()
    service = CorporateActionService(repository)
    early_observation = OBSERVED - timedelta(days=2)
    first = command(
        observed_at=early_observation,
        fetched_at=early_observation + timedelta(minutes=1),
        facts=(fact(observed_at=early_observation),),
    )
    revised = command(
        facts=(
            fact(
                adjustment_factor=Decimal("0.8"),
                raw_content_hash="b" * 64,
            ),
        )
    )
    asyncio.run(service.record_fetch(first))
    asyncio.run(service.record_fetch(revised))

    before_revision = query(service, as_of=early_observation + timedelta(hours=1))
    after_revision = query(service)

    assert before_revision.entries[0].adjustment_factor == Decimal("0.9")
    assert after_revision.entries[0].adjustment_factor == Decimal("0.8")
    assert [stored.revision_no for stored in repository.facts] == [1, 2]


def test_same_day_events_are_multiplied_in_stable_order() -> None:
    repository = MemoryCorporateActionRepository()
    service = CorporateActionService(repository)
    fetch = command(
        facts=(
            fact(source_event_id="event-b", adjustment_factor=Decimal("0.5")),
            fact(
                source_event_id="event-a",
                adjustment_factor=Decimal("0.8"),
                raw_content_hash="b" * 64,
            ),
        )
    )
    asyncio.run(service.record_fetch(fetch))

    timeline = query(service)

    assert len(timeline.entries) == 1
    assert timeline.entries[0].adjustment_factor == Decimal("0.40")
    assert timeline.entries[0].effective_date == date(2026, 1, 10)


def test_missing_successful_coverage_fails_closed() -> None:
    service = CorporateActionService(MemoryCorporateActionRepository())

    with pytest.raises(AppError) as error:
        query(service)

    assert error.value.code == "ADJUSTMENT_DATA_UNAVAILABLE"


def test_same_source_revision_conflict_fails_closed() -> None:
    repository = MemoryCorporateActionRepository()
    service = CorporateActionService(repository)
    fetch = command(facts=(fact(),))
    asyncio.run(service.record_fetch(fetch))
    repository.facts.append(
        CorporateActionFact(
            batch_id=fetch.batch_id,
            security_id=SECURITY_ID,
            source="EASTMONEY",
            source_event_id="event-1",
            event_type="DIVIDEND",
            event_date=date(2026, 1, 9),
            effective_date=date(2026, 1, 10),
            published_at=datetime(2026, 1, 2, tzinfo=UTC),
            observed_at=OBSERVED,
            revision_no=1,
            adjustment_factor=Decimal("0.8"),
            source_reference="provider:event-1",
            raw_content_hash="b" * 64,
        )
    )

    with pytest.raises(AppError) as error:
        query(service)

    assert error.value.code == "ADJUSTMENT_DATA_UNAVAILABLE"


def test_revision_published_after_effective_date_is_not_used() -> None:
    repository = MemoryCorporateActionRepository()
    service = CorporateActionService(repository)
    asyncio.run(service.record_fetch(command(facts=(fact(),))))
    fetch = command(
        facts=(
            fact(
                published_at=datetime(2026, 1, 11, tzinfo=UTC),
                observed_at=datetime(2026, 1, 12, tzinfo=UTC),
                adjustment_factor=Decimal("0.8"),
                raw_content_hash="b" * 64,
            ),
        ),
        observed_at=datetime(2026, 1, 12, tzinfo=UTC),
        fetched_at=datetime(2026, 1, 12, 0, 1, tzinfo=UTC),
    )
    asyncio.run(service.record_fetch(fetch))

    timeline = query(service, as_of=datetime(2026, 1, 13, tzinfo=UTC))

    assert timeline.entries[0].adjustment_factor == Decimal("0.9")


def test_record_fetch_is_idempotent_and_rejects_changed_replay() -> None:
    repository = MemoryCorporateActionRepository()
    service = CorporateActionService(repository)
    fetch = command(facts=(fact(),))

    first = asyncio.run(service.record_fetch(fetch))
    second = asyncio.run(service.record_fetch(fetch))

    assert first == second
    assert repository.flush_calls == 1

    changed = command(
        batch_id=fetch.batch_id,
        facts=(fact(adjustment_factor=Decimal("0.8")),),
    )
    with pytest.raises(AppError) as error:
        asyncio.run(service.record_fetch(changed))
    assert error.value.code == "ADJUSTMENT_DATA_UNAVAILABLE"


def test_concurrent_identical_fetch_is_treated_as_replay() -> None:
    repository = MemoryCorporateActionRepository()
    service = CorporateActionService(repository)
    fetch = command(facts=(fact(),))
    digest_source = MemoryCorporateActionRepository()
    asyncio.run(CorporateActionService(digest_source).record_fetch(fetch))
    repository.concurrent_batch = digest_source.batches[fetch.batch_id]

    result = asyncio.run(service.record_fetch(fetch))

    assert result == fetch.batch_id
    assert repository.flush_calls == 1


def test_same_event_content_is_reused_across_fetch_batches() -> None:
    repository = MemoryCorporateActionRepository()
    service = CorporateActionService(repository)
    asyncio.run(service.record_fetch(command(facts=(fact(),))))
    later = command(
        observed_at=OBSERVED + timedelta(days=1),
        fetched_at=OBSERVED + timedelta(days=1, minutes=1),
        facts=(fact(observed_at=OBSERVED + timedelta(days=1)),),
    )

    asyncio.run(service.record_fetch(later))

    assert len(repository.batches) == 2
    assert len(repository.facts) == 1
    assert repository.facts[0].revision_no == 1


def test_concurrent_new_revision_retries_with_next_revision_number() -> None:
    repository = MemoryCorporateActionRepository()
    service = CorporateActionService(repository)
    asyncio.run(service.record_fetch(command(facts=(fact(),))))
    race_batch = command(
        observed_at=OBSERVED + timedelta(hours=1),
        fetched_at=OBSERVED + timedelta(hours=1, minutes=1),
    )
    repository.concurrent_batch = CorporateActionFetchBatch(
        id=race_batch.batch_id,
        security_id=SECURITY_ID,
        source="EASTMONEY",
        provider_contract_version="corporate-actions-v1",
        coverage_start=date(2026, 1, 1),
        coverage_end=date(2026, 1, 31),
        observed_at=race_batch.observed_at,
        fetched_at=race_batch.fetched_at,
        status="SUCCESS",
        row_count=1,
        content_hash="c" * 64,
        error_code=None,
    )
    repository.concurrent_fact = CorporateActionFact(
        batch_id=race_batch.batch_id,
        security_id=SECURITY_ID,
        source="EASTMONEY",
        source_event_id="event-1",
        event_type="DIVIDEND",
        event_date=date(2026, 1, 9),
        effective_date=date(2026, 1, 10),
        published_at=datetime(2026, 1, 2, tzinfo=UTC),
        observed_at=race_batch.observed_at,
        revision_no=2,
        adjustment_factor=Decimal("0.85"),
        source_reference="provider:event-1",
        raw_content_hash="c" * 64,
    )
    ours = command(
        observed_at=OBSERVED + timedelta(hours=2),
        fetched_at=OBSERVED + timedelta(hours=2, minutes=1),
        facts=(fact(adjustment_factor=Decimal("0.8"), raw_content_hash="b" * 64),),
    )

    asyncio.run(service.record_fetch(ours))

    revisions = sorted(
        (stored.revision_no, stored.raw_content_hash) for stored in repository.facts
    )
    assert revisions == [(1, "a" * 64), (2, "c" * 64), (3, "b" * 64)]


@pytest.mark.parametrize(
    "fetch",
    [
        command(succeeded=False, error_code=None),
        command(succeeded=False, error_code="UPSTREAM_FAILED", facts=(fact(),)),
        command(facts=(fact(adjustment_factor=Decimal("0")),)),
        command(facts=(fact(raw_content_hash="invalid"),)),
    ],
)
def test_invalid_fetch_evidence_is_rejected(fetch: RecordCorporateActionFetch) -> None:
    service = CorporateActionService(MemoryCorporateActionRepository())

    with pytest.raises(AppError) as error:
        asyncio.run(service.record_fetch(fetch))

    assert error.value.code == "ADJUSTMENT_DATA_UNAVAILABLE"
