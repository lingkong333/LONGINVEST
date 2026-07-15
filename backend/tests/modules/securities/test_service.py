from datetime import date
from unittest.mock import AsyncMock, Mock

import pytest

from long_invest.modules.securities.contracts import (
    ListingStatus,
    Market,
    SecurityMasterItem,
    SecurityMasterSnapshot,
    SecurityType,
    UniverseQuery,
)
from long_invest.modules.securities.models import Security, SecurityMasterVersion
from long_invest.modules.securities.service import SecurityMasterService
from long_invest.platform.errors import AppError


def item(
    symbol: str = "600000.SH",
    *,
    name: str = "浦发银行",
    provider_codes: dict[str, str] | None = None,
    status: ListingStatus = ListingStatus.LISTED,
) -> SecurityMasterItem:
    return SecurityMasterItem(
        symbol=symbol,
        exchange_code=symbol[:6],
        name=name,
        market=Market(symbol[-2:]),
        security_type=SecurityType.A_SHARE,
        listing_status=status,
        listed_on=date(1999, 11, 10),
        delisted_on=date(2025, 1, 1) if status is ListingStatus.DELISTED else None,
        is_st=False,
        is_suspended=False,
        provider_codes=(
            provider_codes
            if provider_codes is not None
            else {"eastmoney": f"1.{symbol[:6]}", "sina": f"sh{symbol[:6]}"}
        ),
    )


def snapshot(
    *items: SecurityMasterItem,
    source_version: str = "2026-07-15T09:00:00Z",
    key: str = "refresh-1",
) -> SecurityMasterSnapshot:
    return SecurityMasterSnapshot(
        source="eastmoney",
        source_version=source_version,
        idempotency_key=key,
        items=tuple(items),
    )


class FakeRepository:
    def __init__(self) -> None:
        self.securities: dict[str, Security] = {}
        self.imports_by_key: dict[tuple[str, str], SecurityMasterVersion] = {}
        self.imports_by_version: dict[tuple[str, str], SecurityMasterVersion] = {}
        self.revisions = []
        self.saved_universes = []
        self.flushed = 0
        self.master_lock_count = 0
        self.read_trace = []

    async def lock_master_updates(self):
        self.master_lock_count += 1
        self.read_trace.append("lock")

    async def find_master_import(
        self, *, source, idempotency_key=None, source_version=None
    ):
        if idempotency_key is not None:
            return self.imports_by_key.get((source, idempotency_key))
        return self.imports_by_version.get((source, source_version))

    async def current_master_version(self):
        self.read_trace.append("version")
        versions = [record.master_version for record in self.imports_by_key.values()]
        return max(versions, default=0)

    def add_master_import(self, record):
        self.imports_by_key[(record.source, record.idempotency_key)] = record
        self.imports_by_version[(record.source, record.source_version)] = record

    async def get_many(self, symbols):
        return {
            symbol: self.securities[symbol]
            for symbol in symbols
            if symbol in self.securities
        }

    async def list_all_for_update(self):
        return sorted(self.securities.values(), key=lambda security: security.symbol)

    def add_security(self, security):
        self.securities[security.symbol] = security

    async def next_revision_no(self, _security_id):
        return len(self.revisions) + 1

    def add_revision(self, revision):
        self.revisions.append(revision)

    async def list_for_universe(self, _query):
        self.read_trace.append("securities")
        return sorted(self.securities.values(), key=lambda security: security.symbol)

    async def save_universe_snapshot(self, frozen, items):
        self.saved_universes.append((frozen, items))

    async def get_by_symbol(self, symbol, *, lock=False):
        return self.securities.get(symbol)

    async def flush(self):
        self.flushed += 1


def service(repository: FakeRepository):
    session = Mock()
    session.commit = AsyncMock()
    session.add = Mock()
    return SecurityMasterService(session, repository=repository), session


@pytest.mark.anyio
async def test_first_snapshot_creates_securities_and_emits_one_update_event() -> None:
    repository = FakeRepository()
    subject, session = service(repository)

    result = await subject.apply_snapshot(snapshot(item()))

    assert result.master_version == 1
    assert result.created_count == 1
    assert result.revision_count == 0
    assert set(repository.securities) == {"600000.SH"}
    event = session.add.call_args.args[0]
    assert event.topic == "security_master.updated"
    assert event.payload["master_version"] == 1
    assert repository.master_lock_count == 1
    session.commit.assert_not_awaited()


@pytest.mark.anyio
async def test_same_content_replay_returns_existing_result_without_revision() -> None:
    repository = FakeRepository()
    subject, session = service(repository)
    first = snapshot(item())
    await subject.apply_snapshot(first)
    session.add.reset_mock()

    result = await subject.apply_snapshot(first)

    assert result.replayed is True
    assert result.master_version == 1
    assert repository.revisions == []
    session.add.assert_not_called()


@pytest.mark.anyio
async def test_same_source_version_with_a_new_key_has_one_formal_result() -> None:
    repository = FakeRepository()
    subject, session = service(repository)
    first = snapshot(item())
    await subject.apply_snapshot(first)
    session.add.reset_mock()

    replay = await subject.apply_snapshot(
        snapshot(item(), key="a-second-request-key")
    )

    assert replay.replayed is True
    assert replay.master_version == 1
    assert len(repository.imports_by_version) == 1
    session.add.assert_not_called()


@pytest.mark.anyio
async def test_missing_security_is_delisted_once_and_replay_adds_no_revision() -> None:
    repository = FakeRepository()
    subject, _session = service(repository)
    await subject.apply_snapshot(
        snapshot(item(), item("000001.SZ", name="平安银行"))
    )

    reduced = snapshot(item(), source_version="v2", key="key-2")
    result = await subject.apply_snapshot(reduced)

    missing = repository.securities["000001.SZ"]
    assert missing.listing_status == ListingStatus.DELISTED
    assert result.revision_count == 1
    assert repository.revisions[-1].changed_fields == ["listing_status"]
    eligibility = await subject.validate_monitoring_eligibility("000001.SZ")
    assert eligibility.code == "SECURITY_DELISTED"

    revision_count = len(repository.revisions)
    replay = await subject.apply_snapshot(reduced)
    assert replay.replayed is True
    assert len(repository.revisions) == revision_count
    assert repository.master_lock_count == 3


@pytest.mark.anyio
async def test_later_snapshot_can_restore_a_previously_missing_security() -> None:
    repository = FakeRepository()
    subject, _session = service(repository)
    restored_item = item("000001.SZ", name="平安银行")
    await subject.apply_snapshot(snapshot(item(), restored_item))
    await subject.apply_snapshot(
        snapshot(item(), source_version="v2", key="key-2")
    )

    result = await subject.apply_snapshot(
        snapshot(item(), restored_item, source_version="v3", key="key-3")
    )

    restored = repository.securities["000001.SZ"]
    assert restored.listing_status == ListingStatus.LISTED
    assert result.revision_count == 1
    assert repository.revisions[-1].changed_fields == ["listing_status"]


@pytest.mark.anyio
async def test_real_field_change_appends_revision_with_before_and_after() -> None:
    repository = FakeRepository()
    subject, _session = service(repository)
    await subject.apply_snapshot(snapshot(item()))

    result = await subject.apply_snapshot(
        snapshot(item(name="浦发银行股份有限公司"), source_version="v2", key="key-2")
    )

    assert result.updated_count == 1
    assert result.revision_count == 1
    revision = repository.revisions[0]
    assert revision.changed_fields == ["name"]
    assert revision.before_data["name"] == "浦发银行"
    assert revision.after_data["name"] == "浦发银行股份有限公司"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid_snapshot",
    [
        snapshot(),
        snapshot(item(), item()),
        snapshot(item(provider_codes={})),
        snapshot(item(provider_codes={"eastmoney": ""})),
    ],
)
async def test_invalid_snapshot_fails_before_any_mutation(invalid_snapshot) -> None:
    repository = FakeRepository()
    subject, session = service(repository)

    with pytest.raises(AppError) as captured:
        await subject.apply_snapshot(invalid_snapshot)

    assert captured.value.code == "SECURITY_SNAPSHOT_INCOMPLETE"
    assert repository.securities == {}
    assert repository.imports_by_key == {}
    session.add.assert_not_called()


@pytest.mark.anyio
async def test_different_content_reusing_key_is_conflict() -> None:
    repository = FakeRepository()
    subject, _session = service(repository)
    await subject.apply_snapshot(snapshot(item()))

    with pytest.raises(AppError) as captured:
        await subject.apply_snapshot(snapshot(item(name="另一个名称")))

    assert captured.value.status_code == 409
    assert captured.value.code == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.anyio
async def test_freeze_universe_copies_current_state_and_filter_version() -> None:
    repository = FakeRepository()
    subject, _session = service(repository)
    await subject.apply_snapshot(snapshot(item()))

    frozen = await subject.freeze_universe(UniverseQuery(include_st=False))

    assert frozen.item_count == 1
    assert frozen.master_version == 1
    assert frozen.filters["include_st"] is False
    frozen_item = repository.saved_universes[0][1][0]
    assert frozen_item.symbol == "600000.SH"
    assert frozen_item.master_version == 1
    assert repository.master_lock_count == 2
    assert repository.read_trace[-3:] == ["lock", "securities", "version"]


@pytest.mark.anyio
async def test_eligibility_uses_persisted_security_and_stable_errors() -> None:
    repository = FakeRepository()
    subject, _session = service(repository)

    with pytest.raises(AppError) as malformed:
        await subject.validate_monitoring_eligibility("600000")
    assert malformed.value.code == "SECURITY_SYMBOL_INVALID"

    with pytest.raises(AppError) as missing:
        await subject.validate_monitoring_eligibility("600000.SH")
    assert missing.value.code == "SECURITY_NOT_FOUND"

    await subject.apply_snapshot(snapshot(item(status=ListingStatus.DELISTED)))
    result = await subject.validate_monitoring_eligibility("600000.SH")
    assert result.code == "SECURITY_DELISTED"
