from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import unified_diff
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from long_invest.modules.strategies.models import (
    Strategy,
    StrategyDraft,
    StrategyDraftRevision,
    StrategyVersion,
)
from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.errors import AppError

MAX_SOURCE_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class StrategyCommandContext:
    request_id: str
    idempotency_key: str
    actor_user_id: str
    session_id: str
    trusted_ip: str
    reason: str


@dataclass(frozen=True, slots=True)
class PublishEvidence:
    validation_run_id: UUID
    source_code_hash: str
    metadata: dict[str, Any]
    parameter_schema: dict[str, Any]
    environment_version: str
    runner_image_digest: str


@dataclass(frozen=True, slots=True)
class StrategyCreated:
    strategy: Strategy
    draft: StrategyDraft


@dataclass(frozen=True, slots=True)
class FrozenPublication:
    version: StrategyVersion
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class StrategyEvent:
    topic: str
    strategy_id: UUID
    dedupe_key: str
    payload: dict[str, Any]


class AuditPort(Protocol):
    async def append(self, item: AuditWrite) -> Any: ...

    async def find_by_idempotency(self, key: str) -> Any | None: ...


class EventPort(Protocol):
    async def emit(self, item: StrategyEvent) -> Any: ...


class StrategyService:
    def __init__(self, repository: Any, *, audit: AuditPort, events: EventPort) -> None:
        self._repository = repository
        self._audit = audit
        self._events = events

    async def list(
        self, *, page: int, page_size: int, include_archived: bool
    ) -> tuple[list[Strategy], int]:
        _validate_page(page, page_size)
        return await self._repository.list_strategies(
            page=page, page_size=page_size, include_archived=include_archived
        )

    async def get(self, strategy_id: UUID) -> Strategy:
        strategy = await self._repository.get_strategy(strategy_id)
        if strategy is None:
            raise _not_found()
        return strategy

    async def get_draft(self, strategy_id: UUID) -> StrategyDraft:
        await self.get(strategy_id)
        draft = await self._repository.get_draft(strategy_id)
        if draft is None:
            raise _not_found()
        return draft

    async def list_revisions(
        self, strategy_id: UUID, *, page: int, page_size: int
    ) -> tuple[list[StrategyDraftRevision], int]:
        _validate_page(page, page_size)
        await self.get(strategy_id)
        return await self._repository.list_revisions(
            strategy_id, page=page, page_size=page_size
        )

    async def list_versions(
        self, strategy_id: UUID, *, page: int, page_size: int
    ) -> tuple[list[StrategyVersion], int]:
        _validate_page(page, page_size)
        await self.get(strategy_id)
        return await self._repository.list_versions(
            strategy_id, page=page, page_size=page_size
        )

    async def create(
        self, name: str, context: StrategyCommandContext
    ) -> StrategyCreated:
        name = name.strip()
        _require_context(context)
        if not name or len(name) > 100:
            raise _invalid("策略名称不能为空且不能超过 100 个字符")
        strategy_id = uuid5(
            NAMESPACE_URL,
            f"longinvest:strategy:{context.actor_user_id}:{context.idempotency_key}",
        )
        existing = await self._repository.get_strategy(strategy_id)
        if existing is not None:
            if existing.name != name:
                raise _idempotency_conflict()
            draft = await self._repository.get_draft(strategy_id)
            if draft is None:
                raise _not_found()
            return StrategyCreated(strategy=existing, draft=draft)
        strategy = Strategy(id=strategy_id, name=name, status="DRAFT")
        draft = StrategyDraft(
            id=uuid4(), strategy_id=strategy.id, source_code="", draft_version=1
        )
        await self._repository.create_strategy(strategy, draft)
        await self._record(
            "strategy.created",
            strategy,
            context,
            before=None,
            after={"name": name, "status": "DRAFT", "draft_version": 1},
        )
        return StrategyCreated(strategy=strategy, draft=draft)

    async def save_draft(
        self,
        strategy_id: UUID,
        *,
        source_code: str,
        expected_version: int,
        create_revision: bool,
        context: StrategyCommandContext,
    ) -> StrategyDraft:
        _require_context(context)
        _validate_source(source_code)
        strategy = await self._locked_strategy(strategy_id)
        self._ensure_editable(strategy)
        current = await self._repository.get_draft(strategy_id, for_update=True)
        if current is None:
            raise _not_found()
        topic = (
            "strategy.draft_revision_created"
            if create_revision
            else "strategy.draft_saved"
        )
        replay_key = _audit_key(topic, strategy.id, context.idempotency_key)
        replay = await self._audit.find_by_idempotency(replay_key)
        if replay is not None:
            after = replay.after_summary or {}
            if (
                after.get("source_code_hash") != self.hash_source(source_code)
                or (replay.before_summary or {}).get("draft_version")
                != expected_version
                or bool(after.get("manual_revision")) != create_revision
            ):
                raise _idempotency_conflict()
            if self.hash_source(current.source_code) != after.get("source_code_hash"):
                raise _idempotency_conflict()
            return current
        if current.draft_version != expected_version:
            raise _version_conflict(current)
        before = {
            "draft_version": current.draft_version,
            "source_code_hash": self.hash_source(current.source_code),
        }
        changed = await self._repository.update_draft(
            strategy_id,
            source_code=source_code,
            expected_version=expected_version,
        )
        if changed is None:
            raise _version_conflict(current)
        if strategy.status in {"VALIDATED", "PUBLISH_FAILED"}:
            await self._repository.set_strategy_status(strategy_id, "DRAFT")
            strategy.status = "DRAFT"
        if create_revision:
            revision = StrategyDraftRevision(
                id=uuid4(),
                draft_id=changed.id,
                revision_no=await self._repository.next_revision_no(changed.id),
                source_code=changed.source_code,
            )
            await self._repository.add_revision(revision)
        await self._record(
            topic,
            strategy,
            context,
            before=before,
            after={
                "draft_version": changed.draft_version,
                "source_code_hash": self.hash_source(changed.source_code),
                "manual_revision": create_revision,
            },
        )
        return changed

    async def rename(
        self,
        strategy_id: UUID,
        *,
        name: str,
        expected_version: int,
        context: StrategyCommandContext,
    ) -> Strategy:
        _require_context(context)
        name = name.strip()
        if not name or len(name) > 100:
            raise _invalid("策略名称不能为空且不能超过 100 个字符")
        strategy = await self._locked_strategy(strategy_id)
        self._ensure_editable(strategy)
        draft = await self._repository.get_draft(strategy_id, for_update=True)
        if draft is None:
            raise _not_found()
        replay = await self._audit.find_by_idempotency(
            _audit_key("strategy.updated", strategy.id, context.idempotency_key)
        )
        if replay is not None:
            if (
                (replay.after_summary or {}).get("name") != name
                or (replay.before_summary or {}).get("draft_version")
                != expected_version
            ):
                raise _idempotency_conflict()
            return strategy
        if draft.draft_version != expected_version:
            raise _version_conflict(draft)
        before = {"name": strategy.name, "draft_version": draft.draft_version}
        await self._repository.set_strategy_name(strategy_id, name)
        strategy.name = name
        await self._record(
            "strategy.updated",
            strategy,
            context,
            before=before,
            after={"name": name, "draft_version": draft.draft_version},
        )
        return strategy

    async def restore_revision(
        self,
        strategy_id: UUID,
        *,
        revision_id: UUID,
        expected_version: int,
        context: StrategyCommandContext,
    ) -> StrategyDraft:
        source = await self._repository.get_revision(strategy_id, revision_id)
        if source is None:
            raise AppError(
                code="STRATEGY_REVISION_NOT_FOUND",
                message="策略草稿修订不存在",
                status_code=404,
            )
        return await self.save_draft(
            strategy_id,
            source_code=source.source_code,
            expected_version=expected_version,
            create_revision=True,
            context=context,
        )

    async def diff(self, strategy_id: UUID, *, revision_id: UUID) -> str:
        revision = await self._repository.get_revision(strategy_id, revision_id)
        if revision is None:
            raise AppError(
                code="STRATEGY_REVISION_NOT_FOUND",
                message="策略草稿修订不存在",
                status_code=404,
            )
        draft = await self.get_draft(strategy_id)
        return "".join(
            unified_diff(
                revision.source_code.splitlines(keepends=True),
                draft.source_code.splitlines(keepends=True),
                fromfile=f"revision-{revision.revision_no}",
                tofile=f"draft-{draft.draft_version}",
            )
        )

    async def begin_publish(
        self,
        strategy_id: UUID,
        evidence: PublishEvidence,
        context: StrategyCommandContext,
    ) -> FrozenPublication:
        _require_context(context)
        metadata = _json_snapshot(evidence.metadata)
        parameter_schema = _json_snapshot(evidence.parameter_schema)
        if re.fullmatch(r"[0-9a-f]{64}", evidence.source_code_hash) is None:
            raise _invalid("源码哈希无效")
        if (
            not evidence.environment_version.strip()
            or re.fullmatch(
                r"sha256:[0-9a-f]{64}", evidence.runner_image_digest
            )
            is None
        ):
            raise _invalid("策略运行环境或镜像摘要无效")
        strategy = await self._locked_strategy(strategy_id)
        self._ensure_editable(strategy)
        draft = await self._repository.get_draft(strategy_id, for_update=True)
        if draft is None:
            raise _not_found()
        actual_hash = self.hash_source(draft.source_code)
        if evidence.source_code_hash != actual_hash:
            raise AppError(
                code="STRATEGY_VALIDATION_STALE",
                message="草稿已变化，请重新验证后再发布",
                status_code=409,
            )
        validation = await self._repository.get_validation_run(
            evidence.validation_run_id
        )
        if validation is None or str(validation.status) != "SUCCEEDED":
            raise AppError(
                code="STRATEGY_VALIDATION_REQUIRED",
                message="当前源码缺少成功的完整验证",
                status_code=409,
            )
        failed = await self._repository.latest_failed_version(strategy_id, actual_hash)
        if failed is not None:
            if not _same_release(failed, evidence):
                raise AppError(
                    code="STRATEGY_VALIDATION_STALE",
                    message="失败版本的冻结证据与当前发布请求不一致",
                    status_code=409,
                )
            failed.status = "PUBLISHING"
            await self._repository.set_strategy_status(strategy_id, "PUBLISHING")
            strategy.status = "PUBLISHING"
            return FrozenPublication(version=failed, replayed=True)
        if strategy.status == "PUBLISHING":
            raise AppError(
                code="STRATEGY_PUBLISH_IN_PROGRESS",
                message="策略正在发布，请稍后查看结果",
                status_code=409,
            )
        version = StrategyVersion(
            id=uuid4(),
            strategy_id=strategy_id,
            version_no=await self._repository.next_version_no(strategy_id),
            source_code_hash=actual_hash,
            source_code=draft.source_code,
            strategy_metadata=metadata,
            parameter_schema=parameter_schema,
            environment_version=evidence.environment_version,
            runner_image_digest=evidence.runner_image_digest,
            validation_run_id=evidence.validation_run_id,
            status="PUBLISHING",
        )
        await self._repository.add_version(version)
        await self._repository.set_strategy_status(strategy_id, "PUBLISHING")
        strategy.status = "PUBLISHING"
        await self._record(
            "strategy.publish_requested",
            strategy,
            context,
            before={"status": "DRAFT", "draft_version": draft.draft_version},
            after={
                "status": "PUBLISHING",
                "version_id": str(version.id),
                "version_no": version.version_no,
                "source_code_hash": actual_hash,
            },
        )
        return FrozenPublication(version=version)

    async def complete_publish(
        self,
        strategy_id: UUID,
        version_id: UUID,
        *,
        git_commit: str,
        context: StrategyCommandContext,
    ) -> StrategyVersion:
        _require_context(context)
        strategy = await self._locked_strategy(strategy_id)
        version = await self._repository.get_version(
            strategy_id, version_id, for_update=True
        )
        if version is None:
            raise _not_found()
        if version.status == "PUBLISHED":
            return version
        if version.status != "PUBLISHING" or strategy.status != "PUBLISHING":
            raise AppError(
                code="STRATEGY_PUBLISH_FAILED",
                message="策略发布状态不允许完成",
                status_code=409,
            )
        if not 7 <= len(git_commit) <= 64:
            raise _invalid("Git 提交标识无效")
        version.git_commit = git_commit
        version.published_at = _utc_now()
        version.status = "PUBLISHED"
        await self._repository.set_strategy_status(strategy_id, "PUBLISHED")
        strategy.status = "PUBLISHED"
        await self._record(
            "strategy.published",
            strategy,
            context,
            before={"status": "PUBLISHING", "version_id": str(version.id)},
            after={
                "status": "PUBLISHED",
                "version_id": str(version.id),
                "version_no": version.version_no,
                "source_code_hash": version.source_code_hash,
                "git_commit": git_commit,
            },
        )
        return version

    async def fail_publish(
        self,
        strategy_id: UUID,
        version_id: UUID,
        error_code: str,
        *,
        context: StrategyCommandContext,
    ) -> StrategyVersion:
        _require_context(context)
        strategy = await self._locked_strategy(strategy_id)
        version = await self._repository.get_version(
            strategy_id, version_id, for_update=True
        )
        if version is None:
            raise _not_found()
        if version.status == "PUBLISHED":
            return version
        version.status = "PUBLISH_FAILED"
        version.git_commit = None
        version.published_at = None
        await self._repository.set_strategy_status(strategy_id, "PUBLISH_FAILED")
        strategy.status = "PUBLISH_FAILED"
        await self._record(
            "strategy.publish_failed",
            strategy,
            context,
            before={"status": "PUBLISHING", "version_id": str(version.id)},
            after={
                "status": "PUBLISH_FAILED",
                "version_id": str(version.id),
                "error_code": error_code,
            },
        )
        return version

    async def archive(
        self,
        strategy_id: UUID,
        *,
        expected_version: int,
        context: StrategyCommandContext,
    ) -> Strategy:
        _require_context(context)
        strategy = await self._locked_strategy(strategy_id)
        if strategy.status == "ARCHIVED":
            return strategy
        if strategy.status == "PUBLISHING":
            raise AppError(
                code="STRATEGY_PUBLISH_IN_PROGRESS",
                message="发布中的策略不能归档",
                status_code=409,
            )
        draft = await self._repository.get_draft(strategy_id, for_update=True)
        if draft is None:
            raise _not_found()
        if draft.draft_version != expected_version:
            raise _version_conflict(draft)
        before_status = strategy.status
        await self._repository.set_strategy_status(strategy_id, "ARCHIVED")
        strategy.status = "ARCHIVED"
        await self._record(
            "strategy.archived",
            strategy,
            context,
            before={"status": before_status, "draft_version": draft.draft_version},
            after={"status": "ARCHIVED", "draft_version": draft.draft_version},
        )
        return strategy

    async def _locked_strategy(self, strategy_id: UUID) -> Strategy:
        strategy = await self._repository.get_strategy(strategy_id, for_update=True)
        if strategy is None:
            raise _not_found()
        return strategy

    @staticmethod
    def _ensure_editable(strategy: Strategy) -> None:
        if strategy.status == "ARCHIVED":
            raise AppError(
                code="STRATEGY_ARCHIVED",
                message="已归档策略不能修改",
                status_code=409,
            )

    async def _record(
        self,
        topic: str,
        strategy: Strategy,
        context: StrategyCommandContext,
        *,
        before: dict[str, Any] | None,
        after: dict[str, Any],
    ) -> None:
        audit_key = _audit_key(topic, strategy.id, context.idempotency_key)
        await self._audit.append(
            AuditWrite(
                action_code=topic,
                object_type="strategy",
                object_id=str(strategy.id),
                result="SUCCESS",
                request_id=context.request_id,
                idempotency_key=audit_key,
                risk_level="HIGH",
                reason=context.reason,
                before_summary=before,
                after_summary=after,
                actor_user_id=context.actor_user_id,
                session_id=context.session_id,
                trusted_ip=context.trusted_ip,
            )
        )
        await self._events.emit(
            StrategyEvent(
                topic=topic,
                strategy_id=strategy.id,
                dedupe_key=audit_key,
                payload={"event_type": topic, "strategy_id": str(strategy.id), **after},
            )
        )

    @staticmethod
    def hash_source(source_code: str) -> str:
        return hashlib.sha256(source_code.encode()).hexdigest()


def _same_release(version: StrategyVersion, evidence: PublishEvidence) -> bool:
    return (
        version.validation_run_id == evidence.validation_run_id
        and version.strategy_metadata == evidence.metadata
        and version.parameter_schema == evidence.parameter_schema
        and version.environment_version == evidence.environment_version
        and version.runner_image_digest == evidence.runner_image_digest
    )


def _validate_source(source_code: str) -> None:
    if len(source_code.encode()) > MAX_SOURCE_BYTES:
        raise _invalid("策略源码不能超过 256 KB")


def _json_snapshot(value: dict[str, Any]) -> dict[str, Any]:
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise _invalid("发布快照必须是有限的 JSON 基本类型") from exc
    result = json.loads(serialized)
    if not isinstance(result, dict):
        raise _invalid("发布快照必须是 JSON 对象")
    return result


def _require_context(context: StrategyCommandContext) -> None:
    if (
        not context.request_id.strip()
        or not context.idempotency_key.strip()
        or not context.actor_user_id.strip()
        or not context.session_id.strip()
        or not context.trusted_ip.strip()
        or not context.reason.strip()
    ):
        raise _invalid("写操作需要身份、确认原因和幂等信息")


def _validate_page(page: int, page_size: int) -> None:
    if page < 1 or page_size < 1 or page_size > 100:
        raise _invalid("分页参数无效")


def _audit_key(topic: str, strategy_id: UUID, idempotency_key: str) -> str:
    digest = hashlib.sha256(
        f"{topic}\0{strategy_id}\0{idempotency_key}".encode()
    ).hexdigest()
    return f"strategy:{digest}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _version_conflict(draft: StrategyDraft) -> AppError:
    return AppError(
        code="STRATEGY_VERSION_CONFLICT",
        message="草稿版本冲突，请刷新后处理差异",
        status_code=409,
        details={
            "server_draft_version": draft.draft_version,
            "server_source_code": draft.source_code,
        },
    )


def _not_found() -> AppError:
    return AppError(code="STRATEGY_NOT_FOUND", message="策略不存在", status_code=404)


def _invalid(message: str) -> AppError:
    return AppError(code="STRATEGY_INPUT_INVALID", message=message, status_code=422)


def _idempotency_conflict() -> AppError:
    return AppError(
        code="STRATEGY_IDEMPOTENCY_CONFLICT",
        message="同一幂等键已用于不同的策略操作",
        status_code=409,
    )
