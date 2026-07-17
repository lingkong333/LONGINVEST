from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from long_invest.modules.securities.contracts import (
    ListingStatus,
    Market,
    SecurityIdentity,
    SecurityType,
)
from long_invest.modules.watchlists.contracts import WatchlistMutation
from long_invest.modules.watchlists.service import WatchlistService
from long_invest.platform.errors import AppError


@dataclass
class Record:
    id: object
    owner_user_id: object
    name: str = "观察"
    description: str | None = None
    display_order: int = 0
    version: int = 1
    archived_at: object | None = None
    items: tuple = ()


class FakeRepository:
    def __init__(self) -> None:
        self.record = Record(uuid4(), uuid4())
        self.replays: dict[str, tuple[str, object]] = {}
        self.members: dict[object, object] = {}

    async def find_replay(self, key):
        return self.replays.get(key)

    async def get(self, watchlist_id, *, lock=False):
        return self.record if watchlist_id == self.record.id else None

    async def create(self, **values):
        self.record = Record(
            uuid4(),
            values["owner_user_id"],
            values["name"],
            values["description"],
            values["display_order"],
        )
        return self.record

    async def update_version(self, watchlist_id, *, expected_version, **values):
        if self.record.version != expected_version:
            raise AppError(
                code="WATCHLIST_VERSION_CONFLICT", message="conflict", status_code=409
            )
        for key, value in values.items():
            setattr(self.record, key, value)
        self.record.version += 1
        return self.record

    async def archive(self, watchlist_id, *, expected_version):
        if self.record.version != expected_version:
            raise AppError(
                code="WATCHLIST_VERSION_CONFLICT", message="conflict", status_code=409
            )
        self.record.archived_at = object()
        self.record.version += 1
        return self.record

    async def get_item(self, watchlist_id, security_id):
        return self.members.get(security_id)

    async def add_item(self, watchlist_id, *, security_id, symbol, source):
        item = Record(uuid4(), uuid4())
        item.watchlist_id = watchlist_id
        item.security_id = security_id
        item.symbol = symbol
        item.source = source
        self.members[security_id] = item
        return item

    async def remove_item(self, watchlist_id, security_id):
        return self.members.pop(security_id, None)

    async def count_memberships(self, security_id):
        return int(security_id in self.members)


class FakeAudit:
    def __init__(self, repository: FakeRepository) -> None:
        self.repository = repository
        self.calls = []

    async def append(self, event):
        self.calls.append(event)
        self.repository.replays[event.idempotency_key] = (
            event.after_summary["request_hash"],
            UUID(event.object_id),
            event.after_summary,
        )


class FakeEvents:
    def __init__(self, *, fail=False) -> None:
        self.fail = fail
        self.calls = []

    async def updated(self, **values):
        if self.fail:
            raise RuntimeError("outbox failed")
        self.calls.append(values)


def identity():
    return SecurityIdentity(
        security_id=uuid4(),
        symbol="600000.SH",
        market=Market.SH,
        security_type=SecurityType.A_SHARE,
        listing_status=ListingStatus.LISTED,
        is_suspended=False,
        is_st=False,
        listed_on=None,
        delisted_on=None,
        master_version=1,
    )


def mutation(*, key="create-1", expected=None, name="观察"):
    return WatchlistMutation(
        name=name,
        description=None,
        display_order=0,
        reason="用户维护",
        idempotency_key=key,
        expected_version=expected,
    )


@pytest.mark.anyio
async def test_create_replays_same_request_and_rejects_same_key_different_content():
    repository = FakeRepository()
    service = WatchlistService(repository, FakeAudit(repository), FakeEvents())
    owner = uuid4()
    first = await service.create(owner, mutation())
    replay = await service.create(owner, mutation())
    assert replay.id == first.id

    with pytest.raises(AppError) as caught:
        await service.create(owner, mutation(name="不同名称"))
    assert caught.value.status_code == 409


@pytest.mark.anyio
async def test_mutations_enforce_version_archive_and_duplicate_member_rules():
    repository = FakeRepository()
    events = FakeEvents()
    service = WatchlistService(repository, FakeAudit(repository), events)
    owner = repository.record.owner_user_id
    security = identity()

    created = await service.add_item(
        repository.record.id,
        owner_user_id=owner,
        security=security,
        source="manual",
        reason="添加",
        idempotency_key="add-1",
        expected_version=1,
    )
    reused = await service.add_item(
        repository.record.id,
        owner_user_id=owner,
        security=security,
        source="manual",
        reason="添加",
        idempotency_key="add-2",
        expected_version=created.version,
    )
    assert created.created is True
    assert reused.created is False

    with pytest.raises(AppError) as caught:
        await service.archive(
            repository.record.id,
            owner_user_id=owner,
            reason="归档",
            idempotency_key="archive-1",
            expected_version=1,
        )
    assert caught.value.code == "WATCHLIST_VERSION_CONFLICT"


@pytest.mark.anyio
async def test_archive_is_soft_delete_and_emits_one_transactional_event():
    repository = FakeRepository()
    events = FakeEvents()
    service = WatchlistService(repository, FakeAudit(repository), events)
    result = await service.archive(
        repository.record.id,
        owner_user_id=repository.record.owner_user_id,
        reason="不再使用",
        idempotency_key="archive",
        expected_version=1,
    )
    assert result.archived is True
    assert result.version == 2
    assert events.calls == [
        {
            "watchlist_id": repository.record.id,
            "action": "archived",
            "symbol": None,
            "version": 2,
            "reason": "不再使用",
        }
    ]


@pytest.mark.anyio
async def test_removing_last_membership_only_recommends_pause():
    repository = FakeRepository()
    service = WatchlistService(repository, FakeAudit(repository), FakeEvents())
    owner = repository.record.owner_user_id
    security = identity()
    added = await service.add_item(
        repository.record.id,
        owner_user_id=owner,
        security=security,
        source="manual",
        reason="添加",
        idempotency_key="add",
        expected_version=1,
    )
    result = await service.remove_item(
        repository.record.id,
        owner_user_id=owner,
        security_id=security.security_id,
        symbol=security.symbol,
        reason="移除",
        idempotency_key="remove",
        expected_version=added.version,
    )
    assert result.removed is True
    assert result.pause_recommended is True


@pytest.mark.anyio
async def test_add_replay_returns_original_result_even_after_member_was_removed():
    repository = FakeRepository()
    service = WatchlistService(repository, FakeAudit(repository), FakeEvents())
    owner = repository.record.owner_user_id
    security = identity()
    first = await service.add_item(
        repository.record.id,
        owner_user_id=owner,
        security=security,
        source="manual",
        reason="添加",
        idempotency_key="original-add",
        expected_version=1,
    )
    await service.remove_item(
        repository.record.id,
        owner_user_id=owner,
        security_id=security.security_id,
        symbol=security.symbol,
        reason="移除",
        idempotency_key="remove",
        expected_version=2,
    )

    replay = await service.add_item(
        repository.record.id,
        owner_user_id=owner,
        security=security,
        source="manual",
        reason="添加",
        idempotency_key="original-add",
        expected_version=1,
    )
    assert replay == first
    assert security.security_id not in repository.members


@pytest.mark.anyio
async def test_reused_add_is_persisted_and_replays_after_later_removal():
    repository = FakeRepository()
    audit = FakeAudit(repository)
    service = WatchlistService(repository, audit, FakeEvents())
    owner = repository.record.owner_user_id
    security = identity()
    await service.add_item(
        repository.record.id,
        owner_user_id=owner,
        security=security,
        source="manual",
        reason="首次添加",
        idempotency_key="seed-add",
        expected_version=1,
    )
    reused = await service.add_item(
        repository.record.id,
        owner_user_id=owner,
        security=security,
        source="manual",
        reason="重复添加",
        idempotency_key="reused-add",
        expected_version=2,
    )
    await service.remove_item(
        repository.record.id,
        owner_user_id=owner,
        security_id=security.security_id,
        symbol=security.symbol,
        reason="移除",
        idempotency_key="remove-after-reuse",
        expected_version=2,
    )

    replay = await service.add_item(
        repository.record.id,
        owner_user_id=owner,
        security=security,
        source="manual",
        reason="重复添加",
        idempotency_key="reused-add",
        expected_version=2,
    )
    assert reused.created is False
    assert replay == reused
    assert security.security_id not in repository.members


@pytest.mark.anyio
async def test_missing_remove_is_persisted_and_replays_after_later_add():
    repository = FakeRepository()
    service = WatchlistService(repository, FakeAudit(repository), FakeEvents())
    owner = repository.record.owner_user_id
    security = identity()
    missing = await service.remove_item(
        repository.record.id,
        owner_user_id=owner,
        security_id=security.security_id,
        symbol=security.symbol,
        reason="不存在也移除",
        idempotency_key="missing-remove",
        expected_version=1,
    )
    await service.add_item(
        repository.record.id,
        owner_user_id=owner,
        security=security,
        source="manual",
        reason="后来添加",
        idempotency_key="later-add",
        expected_version=1,
    )

    replay = await service.remove_item(
        repository.record.id,
        owner_user_id=owner,
        security_id=security.security_id,
        symbol=security.symbol,
        reason="不存在也移除",
        idempotency_key="missing-remove",
        expected_version=1,
    )
    assert replay == missing
    assert security.security_id in repository.members


@pytest.mark.anyio
async def test_create_replay_returns_original_snapshot_after_later_update():
    repository = FakeRepository()
    service = WatchlistService(repository, FakeAudit(repository), FakeEvents())
    owner = uuid4()
    original = await service.create(owner, mutation(key="create-original"))
    await service.update(
        original.id,
        owner_user_id=owner,
        command=mutation(key="update-later", expected=1, name="后来修改的名称"),
    )
    replay = await service.create(owner, mutation(key="create-original"))
    assert replay == original


@pytest.mark.anyio
async def test_idempotency_key_is_namespaced_and_request_hash_covers_all_inputs():
    repository = FakeRepository()
    audit = FakeAudit(repository)
    service = WatchlistService(repository, audit, FakeEvents())
    owner = repository.record.owner_user_id
    security = identity()
    await service.add_item(
        repository.record.id,
        owner_user_id=owner,
        security=security,
        source="manual",
        reason="添加",
        idempotency_key="same-user-key",
        expected_version=1,
    )
    stored_key = audit.calls[0].idempotency_key
    assert stored_key.startswith(f"watchlists:{owner}:add_item:")
    assert len(stored_key) <= 160

    with pytest.raises(AppError) as caught:
        await service.add_item(
            repository.record.id,
            owner_user_id=owner,
            security=security,
            source="batch",
            reason="不同来源",
            idempotency_key="same-user-key",
            expected_version=1,
        )
    assert caught.value.status_code == 409


@pytest.mark.anyio
async def test_add_replay_ignores_later_security_master_changes():
    repository = FakeRepository()
    service = WatchlistService(repository, FakeAudit(repository), FakeEvents())
    owner = repository.record.owner_user_id
    original_identity = identity()
    original = await service.add_item(
        repository.record.id,
        owner_user_id=owner,
        security=original_identity,
        source="manual",
        reason="添加",
        idempotency_key="master-change-replay",
        expected_version=1,
    )
    changed_identity = SecurityIdentity(
        security_id=original_identity.security_id,
        symbol=original_identity.symbol,
        market=Market.HK,
        security_type=SecurityType.ETF,
        listing_status=ListingStatus.DELISTED,
        is_suspended=False,
        is_st=True,
        listed_on=None,
        delisted_on=None,
        master_version=2,
    )

    replay = await service.add_item(
        repository.record.id,
        owner_user_id=owner,
        security=changed_identity,
        source="manual",
        reason="添加",
        idempotency_key="master-change-replay",
        expected_version=1,
    )
    assert replay == original
    assert len(repository.members) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("market", "security_type"),
    [(Market.HK, SecurityType.A_SHARE), (Market.SH, SecurityType.ETF)],
)
async def test_member_requires_explicit_a_share_market_and_type(market, security_type):
    repository = FakeRepository()
    service = WatchlistService(repository, FakeAudit(repository), FakeEvents())
    security = identity()
    security = SecurityIdentity(
        security_id=security.security_id,
        symbol=security.symbol,
        market=market,
        security_type=security_type,
        listing_status=security.listing_status,
        is_suspended=security.is_suspended,
        is_st=security.is_st,
        listed_on=security.listed_on,
        delisted_on=security.delisted_on,
        master_version=security.master_version,
    )
    with pytest.raises(AppError) as caught:
        await service.add_item(
            repository.record.id,
            owner_user_id=repository.record.owner_user_id,
            security=security,
            source="manual",
            reason="添加",
            idempotency_key=f"reject-{market}-{security_type}",
            expected_version=1,
        )
    assert caught.value.code == "WATCHLIST_ITEM_REJECTED"


@pytest.mark.anyio
async def test_archived_group_rejects_members_and_owner_is_enforced():
    repository = FakeRepository()
    service = WatchlistService(repository, FakeAudit(repository), FakeEvents())
    repository.record.archived_at = object()
    with pytest.raises(AppError) as archived:
        await service.add_item(
            repository.record.id,
            owner_user_id=repository.record.owner_user_id,
            security=identity(),
            source="manual",
            reason="添加",
            idempotency_key="a",
            expected_version=1,
        )
    assert archived.value.code == "WATCHLIST_ARCHIVED"
    with pytest.raises(AppError) as missing:
        await service.archive(
            repository.record.id,
            owner_user_id=uuid4(),
            reason="归档",
            idempotency_key="b",
            expected_version=1,
        )
    assert missing.value.code == "WATCHLIST_NOT_FOUND"
