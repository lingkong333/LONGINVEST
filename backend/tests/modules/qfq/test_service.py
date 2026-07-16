from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from long_invest.modules.qfq.contracts import (
    QfqBarInput,
    QfqDatasetLifecycle,
    QfqFreshness,
    QfqRefreshStatus,
    ValidatedQfqWindow,
)
from long_invest.modules.qfq.models import QfqDataset
from long_invest.modules.qfq.service import QfqRefreshService

NOW = datetime(2026, 7, 16, 10, tzinfo=UTC)
START = date(2026, 7, 15)
END = date(2026, 7, 16)


def _window(checksum: str = "a" * 64) -> ValidatedQfqWindow:
    bars = tuple(
        QfqBarInput(
            trade_date=trade_date,
            open=Decimal("10.000000"),
            high=Decimal("11.000000"),
            low=Decimal("9.000000"),
            close=Decimal("10.500000"),
            volume=100,
            amount=Decimal("1050.0000"),
        )
        for trade_date in (START, END)
    )
    return ValidatedQfqWindow(
        bars=bars,
        anchor_date=END,
        anchor_close=Decimal("10.500000"),
        row_count=2,
        checksum=checksum,
    )


def _run(*, status: str = "VALIDATING") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        job_id=uuid4(),
        security_id=uuid4(),
        symbol="600000.SH",
        requested_start=START,
        requested_end=END,
        as_of_date=END,
        expected_trade_dates=[START.isoformat(), END.isoformat()],
        input_daily_version=3,
        trigger_reason="MANUAL",
        request_id="req-qfq-1",
        idempotency_key="qfq-1",
        request_hash="b" * 64,
        status=status,
        provider="eastmoney",
        candidate_dataset_id=None,
        activated_dataset_id=None,
        row_count=None,
        checksum=None,
        error_code=None,
        retryable=None,
        result_summary=None,
        created_at=NOW,
        updated_at=NOW,
        completed_at=None,
    )


class FakeRepository:
    def __init__(self, run=None):
        self.session = object()
        self.run = run or _run()
        self.datasets: list[QfqDataset] = []
        self.bars = {}
        self.locked: list[UUID] = []

    async def lock_security(self, security_id):
        self.locked.append(security_id)

    async def get_run(self, run_id, *, for_update=False):
        assert run_id == self.run.id
        assert for_update is True
        return self.run

    async def transition_run(self, run_id, *, expected_status, status, **changes):
        assert run_id == self.run.id
        if self.run.status != expected_status:
            raise RuntimeError("transition conflict")
        self.run.status = status
        for key, value in changes.items():
            setattr(self.run, key, value)
        return self.run

    async def current_dataset(self, security_id, *, for_update=False):
        assert for_update is True
        return next(
            (
                item
                for item in self.datasets
                if item.security_id == security_id
                and item.lifecycle == QfqDatasetLifecycle.CURRENT
            ),
            None,
        )

    async def get_dataset(self, dataset_id):
        return next((item for item in self.datasets if item.id == dataset_id), None)

    async def next_version(self, security_id):
        return (
            max(
                (
                    item.version
                    for item in self.datasets
                    if item.security_id == security_id
                ),
                default=0,
            )
            + 1
        )

    async def add_dataset(self, dataset, bars):
        self.datasets.append(dataset)
        self.bars[dataset.id] = tuple(bars)

    async def transition_dataset(
        self, dataset_id, *, expected_lifecycle, lifecycle, **changes
    ):
        dataset = await self.get_dataset(dataset_id)
        assert dataset.lifecycle == expected_lifecycle
        dataset.lifecycle = lifecycle
        for key, value in changes.items():
            setattr(dataset, key, value)

    async def mark_current_stale(self, security_id, *, reason):
        current = await self.current_dataset(security_id, for_update=True)
        if current is not None:
            current.freshness = QfqFreshness.STALE
            current.stale_reason = reason
        return current

    async def flush(self):
        return None


class RecordingEvents:
    def __init__(self, session):
        self.session = session
        self.records = []
        self.fail_write = False

    async def completed(self, run, dataset):
        if self.fail_write:
            raise RuntimeError("outbox failed")
        self.records.append(("qfq_refresh.completed", run, dataset))

    async def failed(self, run, current):
        if self.fail_write:
            raise RuntimeError("outbox failed")
        self.records.append(("qfq_refresh.failed", run, current))


def _service(repo: FakeRepository):
    events = RecordingEvents(repo.session)
    return QfqRefreshService(repo, events=events), events


def _current(repo: FakeRepository, *, version: int = 1, checksum: str = "c" * 64):
    dataset = QfqDataset(
        id=uuid4(),
        security_id=repo.run.security_id,
        symbol=repo.run.symbol,
        version=version,
        requested_start=START,
        requested_end=END,
        actual_start=START,
        actual_end=END,
        as_of_date=END,
        provider="eastmoney",
        provider_contract_version="v1",
        anchor_date=END,
        anchor_close=Decimal("10.5"),
        row_count=2,
        checksum=checksum,
        lifecycle=QfqDatasetLifecycle.CURRENT,
        freshness=QfqFreshness.FRESH,
        created_at=NOW,
        activated_at=NOW,
    )
    repo.datasets.append(dataset)
    return dataset


@pytest.mark.anyio
async def test_first_success_activates_version_one_and_emits_event() -> None:
    repo = FakeRepository()
    service, events = _service(repo)

    activated = await service.activate(
        repo.run.id,
        _window(),
        current_input_daily_version=3,
        provider_contract_version="eastmoney-v1",
        now=NOW,
    )

    assert activated is not None and activated.version == 1
    assert activated.id is not None
    assert {bar.dataset_id for bar in repo.bars[activated.id]} == {activated.id}
    assert activated.lifecycle == QfqDatasetLifecycle.CURRENT
    assert repo.run.status == QfqRefreshStatus.SUCCEEDED
    assert events.records[-1][0] == "qfq_refresh.completed"


@pytest.mark.anyio
async def test_success_replaces_old_current_atomically() -> None:
    repo = FakeRepository()
    first = _current(repo)
    service, _events = _service(repo)

    second = await service.activate(
        repo.run.id,
        _window(),
        current_input_daily_version=3,
        provider_contract_version="eastmoney-v1",
        now=NOW,
    )

    assert second is not None and second.version == 2
    assert first.lifecycle == QfqDatasetLifecycle.SUPERSEDED
    assert first.superseded_at == NOW
    assert second.lifecycle == QfqDatasetLifecycle.CURRENT
    assert sum(d.lifecycle == QfqDatasetLifecycle.CURRENT for d in repo.datasets) == 1


@pytest.mark.anyio
async def test_duplicate_content_reuses_current_dataset_without_new_version() -> None:
    repo = FakeRepository()
    current = _current(repo, checksum="a" * 64)
    current.freshness = QfqFreshness.STALE
    current.stale_reason = "QFQ_PROVIDER_FAILED"
    service, _events = _service(repo)

    activated = await service.activate(
        repo.run.id,
        _window("a" * 64),
        current_input_daily_version=3,
        provider_contract_version="eastmoney-v1",
        now=NOW,
    )

    assert activated is current
    assert len(repo.datasets) == 1
    assert repo.run.activated_dataset_id == current.id
    assert repo.run.candidate_dataset_id == current.id
    assert current.freshness == QfqFreshness.FRESH
    assert current.stale_reason is None


@pytest.mark.anyio
async def test_superseded_input_never_changes_current_dataset() -> None:
    repo = FakeRepository()
    current = _current(repo)
    service, events = _service(repo)

    activated = await service.activate(
        repo.run.id,
        _window(),
        current_input_daily_version=4,
        provider_contract_version="eastmoney-v1",
        now=NOW,
    )

    assert activated is None
    assert current.lifecycle == QfqDatasetLifecycle.CURRENT
    assert repo.run.status == QfqRefreshStatus.SUPERSEDED
    assert repo.run.error_code == "QFQ_INPUT_SUPERSEDED"
    assert events.records[-1][0] == "qfq_refresh.failed"


@pytest.mark.anyio
async def test_failed_refresh_keeps_current_and_marks_it_stale() -> None:
    repo = FakeRepository(_run(status="FETCHING"))
    current = _current(repo)
    service, events = _service(repo)

    result = await service.fail(
        repo.run.id,
        code="QFQ_PROVIDER_FAILED",
        retryable=True,
        now=NOW,
    )

    assert result is current
    assert current.lifecycle == QfqDatasetLifecycle.CURRENT
    assert current.freshness == QfqFreshness.STALE
    assert current.stale_reason == "QFQ_PROVIDER_FAILED"
    assert repo.run.status == QfqRefreshStatus.FAILED
    assert events.records[-1][0] == "qfq_refresh.failed"


@pytest.mark.anyio
async def test_outbox_failure_propagates_to_transaction_owner() -> None:
    repo = FakeRepository()
    service, events = _service(repo)
    events.fail_write = True

    with pytest.raises(RuntimeError, match="outbox failed"):
        await service.activate(
            repo.run.id,
            _window(),
            current_input_daily_version=3,
            provider_contract_version="eastmoney-v1",
            now=NOW,
        )


@pytest.mark.anyio
async def test_service_rejects_event_writer_from_another_transaction() -> None:
    repo = FakeRepository()
    events = RecordingEvents(object())

    with pytest.raises(Exception) as captured:
        QfqRefreshService(repo, events=events)

    assert captured.value.code == "QFQ_TRANSACTION_MISMATCH"
