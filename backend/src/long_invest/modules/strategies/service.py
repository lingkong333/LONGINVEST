from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from difflib import unified_diff
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from long_invest.modules.strategies.contracts import (
    StrategySubscriptionScope,
    StrategyVersionOperation,
)
from long_invest.modules.strategies.models import (
    Strategy,
    StrategyDraft,
    StrategyDraftRevision,
    StrategyRun,
    StrategyValidationRun,
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
    expected_draft_version: int
    evidence_hash: str | None = None


@dataclass(frozen=True, slots=True)
class StrategyCreated:
    strategy: Strategy
    draft: StrategyDraft


@dataclass(frozen=True, slots=True)
class FrozenPublication:
    version: StrategyVersion
    run: StrategyRun | None = None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class FrozenVersionOperation:
    strategy_version_id: UUID
    subscription_ids: tuple[UUID, ...]
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

    async def get_draft_by_id(self, draft_id: UUID) -> StrategyDraft:
        draft = await self._repository.get_draft_by_id(draft_id)
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

    async def get_published_version_by_id(
        self, strategy_version_id: UUID
    ) -> StrategyVersion | None:
        version = await self._repository.get_version_by_id(strategy_version_id)
        if version is None or str(version.status) not in {"PUBLISHED", "ARCHIVED"}:
            return None
        return version

    async def get_validation_evidence(
        self, validation_run_id: UUID
    ) -> StrategyValidationRun:
        run = await self.get_validation_run(validation_run_id)
        if str(run.status) != "SUCCEEDED":
            raise AppError(
                code="STRATEGY_VALIDATION_REQUIRED",
                message="发布需要已成功的完整验证",
                status_code=409,
            )
        _validated_release_snapshot(run.evidence_snapshot)
        return run

    async def get_validation_run(
        self, validation_run_id: UUID
    ) -> StrategyValidationRun:
        run = await self._repository.get_validation_run(validation_run_id)
        if run is None:
            raise AppError(
                code="STRATEGY_VALIDATION_NOT_FOUND",
                message="策略验证记录不存在",
                status_code=404,
            )
        return run

    async def request_version_operation(
        self,
        strategy_id: UUID,
        *,
        strategy_version_id: UUID,
        operation: StrategyVersionOperation,
        scope: StrategySubscriptionScope,
        subscription_ids: tuple[UUID, ...],
        target_date: date,
        training_start_date: date,
        training_end_date: date,
        context: StrategyCommandContext,
    ) -> FrozenVersionOperation:
        _require_context(context)
        if len(set(subscription_ids)) != len(subscription_ids):
            raise AppError(
                code="STRATEGY_SUBSCRIPTION_SCOPE_INVALID",
                message="策略操作必须包含不重复的订阅",
                status_code=422,
            )
        strategy = await self.get(strategy_id)
        version = await self._repository.get_version(
            strategy_id, strategy_version_id, for_update=False
        )
        if version is None:
            raise _not_found()
        if str(version.status) != "PUBLISHED":
            raise AppError(
                code="STRATEGY_VERSION_NOT_PUBLISHED",
                message="只能应用或回滚到已发布的策略版本",
                status_code=409,
            )
        ordered_ids = tuple(sorted(subscription_ids, key=str))
        topic = (
            "strategy.version_apply_requested"
            if operation is StrategyVersionOperation.APPLY
            else "strategy.version_rollback_requested"
        )
        audit_key = _audit_key(topic, strategy.id, context.idempotency_key)
        replay = await self._audit.find_by_idempotency(audit_key)
        if replay is not None:
            after = replay.after_summary or {}
            try:
                replay_version_id = UUID(str(after["strategy_version_id"]))
                replay_subscription_ids = tuple(
                    UUID(str(value)) for value in after["subscription_ids"]
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise _idempotency_conflict() from exc
            if (
                replay_version_id != strategy_version_id
                or after.get("operation") != operation.value
                or after.get("scope") != scope.value
                or (
                    scope is StrategySubscriptionScope.SELECTED
                    and replay_subscription_ids != ordered_ids
                )
                or after.get("target_date") != target_date.isoformat()
                or after.get("training_start_date") != training_start_date.isoformat()
                or after.get("training_end_date") != training_end_date.isoformat()
            ):
                raise _idempotency_conflict()
            return FrozenVersionOperation(
                strategy_version_id=replay_version_id,
                subscription_ids=replay_subscription_ids,
                replayed=True,
            )
        await self._record(
            topic,
            strategy,
            context,
            before=None,
            after={
                "operation": operation.value,
                "scope": scope.value,
                "strategy_version_id": str(strategy_version_id),
                "subscription_ids": [str(value) for value in ordered_ids],
                "target_date": target_date.isoformat(),
                "training_start_date": training_start_date.isoformat(),
                "training_end_date": training_end_date.isoformat(),
            },
        )
        return FrozenVersionOperation(
            strategy_version_id=strategy_version_id,
            subscription_ids=ordered_ids,
        )

    async def list_recoverable_publish_runs(self) -> list[StrategyRun]:
        return await self._repository.list_recoverable_publish_runs()

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
        if strategy.status in {"VALIDATING", "VALIDATED", "PUBLISH_FAILED"}:
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
                or (replay.after_summary or {}).get("draft_version")
                != expected_version + 1
                or strategy.name != name
                or draft.draft_version != expected_version + 1
            ):
                raise _idempotency_conflict()
            return strategy
        if draft.draft_version != expected_version:
            raise _version_conflict(draft)
        before = {"name": strategy.name, "draft_version": draft.draft_version}
        changed = await self._repository.rename_strategy(
            strategy_id, name=name, expected_version=expected_version
        )
        if changed is None:
            raise _version_conflict(draft)
        strategy.name = name
        if strategy.status in {"VALIDATING", "VALIDATED", "PUBLISH_FAILED"}:
            await self._repository.set_strategy_status(strategy_id, "DRAFT")
            strategy.status = "DRAFT"
        await self._record(
            "strategy.updated",
            strategy,
            context,
            before=before,
            after={
                "name": name,
                "draft_version": changed.draft_version,
                "status": str(strategy.status),
            },
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

    async def request_validation(
        self,
        strategy_id: UUID,
        *,
        backtest_task_id: UUID,
        metadata: dict[str, Any],
        parameter_schema: dict[str, Any],
        params: dict[str, Any],
        environment_version: str,
        runner_image_digest: str,
        context: StrategyCommandContext,
    ) -> StrategyValidationRun:
        _require_context(context)
        metadata = _json_snapshot(metadata)
        parameter_schema = _json_snapshot(parameter_schema)
        params = _json_snapshot(params)
        _validate_environment(environment_version, runner_image_digest)
        strategy = await self._locked_strategy(strategy_id)
        self._ensure_editable(strategy)
        if strategy.status == "PUBLISHING":
            raise AppError(
                code="STRATEGY_PUBLISH_IN_PROGRESS",
                message="发布中的策略不能重新验证",
                status_code=409,
            )
        before_status = str(strategy.status)
        draft = await self._repository.get_draft(strategy_id, for_update=True)
        if draft is None:
            raise _not_found()
        source_code_hash = self.hash_source(draft.source_code)
        frozen_facts = {
            "schema_version": 1,
            "backtest_task_id": str(backtest_task_id),
            "draft_version": draft.draft_version,
            "source_code_hash": source_code_hash,
            "metadata_hash": _json_hash(metadata),
            "parameter_schema_hash": _json_hash(parameter_schema),
            "parameter_hash": _json_hash(params),
            "environment_version": environment_version,
            "environment_hash": hashlib.sha256(
                environment_version.encode()
            ).hexdigest(),
            "runner_image_digest": runner_image_digest,
        }
        replay = await self._audit.find_by_idempotency(
            _audit_key(
                "strategy.validation_requested",
                strategy.id,
                context.idempotency_key,
            )
        )
        if replay is not None:
            after = replay.after_summary or {}
            if any(after.get(key) != value for key, value in frozen_facts.items()):
                raise _idempotency_conflict()
            try:
                validation_run_id = UUID(str(after["validation_run_id"]))
            except (KeyError, ValueError) as exc:
                raise _idempotency_conflict() from exc
            existing = await self._repository.get_validation_run(validation_run_id)
            if existing is None:
                raise _idempotency_conflict()
            return existing
        run = StrategyValidationRun(
            id=uuid4(),
            strategy_id=strategy_id,
            draft_version=draft.draft_version,
            source_code_hash=source_code_hash,
            evidence_snapshot={
                "schema_version": 1,
                "source_code_hash": source_code_hash,
                "metadata": metadata,
                "metadata_hash": frozen_facts["metadata_hash"],
                "parameter_schema": parameter_schema,
                "parameter_schema_hash": frozen_facts["parameter_schema_hash"],
                "params": params,
                "parameter_hash": frozen_facts["parameter_hash"],
                "environment_version": environment_version,
                "environment_hash": frozen_facts["environment_hash"],
                "runner_image_digest": runner_image_digest,
                "checks": {},
            },
            status="PENDING",
        )
        await self._repository.add_validation_run(run)
        await self._repository.set_strategy_status(strategy_id, "VALIDATING")
        strategy.status = "VALIDATING"
        await self._record(
            "strategy.validation_requested",
            strategy,
            context,
            before={
                "status": before_status,
                "draft_version": draft.draft_version,
            },
            after={
                "status": "VALIDATING",
                "validation_run_id": str(run.id),
                **frozen_facts,
            },
        )
        return run

    async def complete_validation(
        self,
        validation_run_id: UUID,
        *,
        succeeded: bool,
        error_code: str | None,
        evidence_snapshot: dict[str, Any],
        context: StrategyCommandContext,
    ) -> StrategyValidationRun:
        _require_context(context)
        run = await self._repository.get_validation_run(
            validation_run_id, for_update=True
        )
        if run is None:
            raise AppError(
                code="STRATEGY_VALIDATION_NOT_FOUND",
                message="策略验证记录不存在",
                status_code=404,
            )
        if succeeded and error_code is not None:
            raise _invalid("成功验证不能包含错误码")
        if not succeeded and not (error_code or "").strip():
            raise _invalid("失败验证必须包含错误码")
        completed_snapshot = _completed_validation_snapshot(
            run,
            succeeded=succeeded,
            evidence=evidence_snapshot,
        )
        if str(run.status) in {"SUCCEEDED", "FAILED"}:
            expected_status = "SUCCEEDED" if succeeded else "FAILED"
            if (
                str(run.status) != expected_status
                or run.error_code != error_code
                or run.evidence_snapshot != completed_snapshot
            ):
                raise _idempotency_conflict()
            return run
        strategy = await self._locked_strategy(run.strategy_id)
        draft = await self._repository.get_draft(run.strategy_id, for_update=True)
        if draft is None:
            raise _not_found()
        status = "SUCCEEDED" if succeeded else "FAILED"
        completed = await self._repository.complete_validation_run(
            validation_run_id,
            status=status,
            error_code=error_code,
            evidence_snapshot=completed_snapshot,
            completed_at=_utc_now(),
        )
        if completed is None:
            raise AppError(
                code="STRATEGY_VALIDATION_CONFLICT",
                message="策略验证状态已被其他执行更新",
                status_code=409,
            )
        is_current = (
            draft.draft_version == run.draft_version
            and self.hash_source(draft.source_code) == run.source_code_hash
        )
        strategy_status = str(strategy.status)
        if is_current:
            strategy_status = "VALIDATED" if succeeded else "DRAFT"
            await self._repository.set_strategy_status(run.strategy_id, strategy_status)
            strategy.status = strategy_status
        topic = (
            "strategy.validation_completed"
            if succeeded
            else "strategy.validation_failed"
        )
        await self._record(
            topic,
            strategy,
            context,
            before={"status": "VALIDATING", "validation_run_id": str(run.id)},
            after={
                "status": strategy_status,
                "validation_status": status,
                "validation_run_id": str(run.id),
                "evidence_is_current": is_current,
                "error_code": error_code,
            },
        )
        return completed

    async def begin_publish(
        self,
        strategy_id: UUID,
        evidence: PublishEvidence,
        context: StrategyCommandContext,
    ) -> FrozenPublication:
        _require_context(context)
        strategy = await self._locked_strategy(strategy_id)
        self._ensure_editable(strategy)
        before_status = str(strategy.status)
        if strategy.status not in {"VALIDATED", "PUBLISH_FAILED", "PUBLISHING"}:
            raise AppError(
                code="STRATEGY_VALIDATION_REQUIRED",
                message="只有完成当前验证的策略才能发布",
                status_code=409,
            )
        draft = await self._repository.get_draft(strategy_id, for_update=True)
        if draft is None:
            raise _not_found()
        if draft.draft_version != evidence.expected_draft_version:
            raise _version_conflict(draft)
        actual_hash = self.hash_source(draft.source_code)
        validation = await self._repository.get_validation_run(
            evidence.validation_run_id
        )
        if validation is None or str(validation.status) != "SUCCEEDED":
            raise AppError(
                code="STRATEGY_VALIDATION_REQUIRED",
                message="当前源码缺少成功的完整验证",
                status_code=409,
            )
        if (
            validation.strategy_id != strategy_id
            or validation.draft_version != draft.draft_version
            or validation.source_code_hash != actual_hash
        ):
            raise AppError(
                code="STRATEGY_VALIDATION_STALE",
                message="验证证据不属于当前策略的当前草稿",
                status_code=409,
            )
        release = _validated_release_snapshot(validation.evidence_snapshot)
        snapshot_hash = _json_hash(validation.evidence_snapshot)
        if evidence.evidence_hash != snapshot_hash:
            raise AppError(
                code="STRATEGY_VALIDATION_STALE",
                message="验证证据未通过发布前事实复核",
                status_code=409,
            )
        if validation.strategy_version_id is not None:
            existing = await self._repository.get_version(
                strategy_id,
                validation.strategy_version_id,
                for_update=True,
            )
            if existing is None or not _same_release(existing, release):
                raise AppError(
                    code="STRATEGY_VALIDATION_STALE",
                    message="验证证据绑定的发布版本不一致",
                    status_code=409,
                )
            run = await self._repository.get_publish_run_for_version(
                existing.id, for_update=True
            )
            if run is None:
                raise AppError(
                    code="STRATEGY_PUBLISH_STATE_UNCERTAIN",
                    message="发布任务记录缺失，版本不可绑定",
                    status_code=503,
                )
            if existing.status == "PUBLISH_FAILED":
                retry_audit_key = _audit_key(
                    "strategy.publish_requested",
                    strategy.id,
                    context.idempotency_key,
                )
                if await self._audit.find_by_idempotency(retry_audit_key) is not None:
                    return FrozenPublication(version=existing, run=run, replayed=True)
                existing.status = "PUBLISHING"
                run.status = "PENDING"
                await self._repository.set_strategy_run_status(run.id, "PENDING")
                await self._repository.set_strategy_status(strategy_id, "PUBLISHING")
                strategy.status = "PUBLISHING"
                await self._record(
                    "strategy.publish_requested",
                    strategy,
                    context,
                    before={
                        "status": "PUBLISH_FAILED",
                        "draft_version": draft.draft_version,
                    },
                    after={
                        "status": "PUBLISHING",
                        "version_id": str(existing.id),
                        "version_no": existing.version_no,
                        "run_id": str(run.id),
                        "source_code_hash": actual_hash,
                        "evidence_hash": snapshot_hash,
                    },
                )
            if existing.status in {"PUBLISHING", "PUBLISHED"}:
                return FrozenPublication(version=existing, run=run, replayed=True)
            raise AppError(
                code="STRATEGY_VERSION_IMMUTABLE",
                message="验证证据已绑定不可重用的发布版本",
                status_code=409,
            )
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
            strategy_metadata=release["metadata"],
            parameter_schema=release["parameter_schema"],
            environment_version=release["environment_version"],
            runner_image_digest=release["runner_image_digest"],
            validation_run_id=evidence.validation_run_id,
            status="PUBLISHING",
        )
        await self._repository.add_version(version)
        run = StrategyRun(
            id=uuid4(),
            strategy_version_id=version.id,
            status="PENDING",
        )
        await self._repository.add_strategy_run(run)
        bound = await self._repository.bind_validation_run(
            evidence.validation_run_id,
            version.id,
            strategy_id=strategy_id,
            draft_version=draft.draft_version,
            source_code_hash=actual_hash,
        )
        if not bound:
            raise AppError(
                code="STRATEGY_VALIDATION_STALE",
                message="验证证据已变化或已绑定其他发布版本",
                status_code=409,
            )
        await self._repository.set_strategy_status(strategy_id, "PUBLISHING")
        strategy.status = "PUBLISHING"
        await self._record(
            "strategy.publish_requested",
            strategy,
            context,
            before={
                "status": before_status,
                "draft_version": draft.draft_version,
            },
            after={
                "status": "PUBLISHING",
                "version_id": str(version.id),
                "version_no": version.version_no,
                "run_id": str(run.id),
                "source_code_hash": actual_hash,
                "evidence_hash": snapshot_hash,
            },
        )
        return FrozenPublication(version=version, run=run)

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
            if version.git_commit != git_commit:
                raise _idempotency_conflict()
            return version
        if version.status != "PUBLISHING" or strategy.status != "PUBLISHING":
            raise AppError(
                code="STRATEGY_PUBLISH_FAILED",
                message="策略发布状态不允许完成",
                status_code=409,
            )
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", git_commit) is None:
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

    async def claim_publish_run(self, run_id: UUID) -> FrozenPublication:
        run = await self._repository.get_strategy_run(run_id, for_update=True)
        if run is None:
            raise _not_found()
        version = await self._repository.get_version_by_id(
            run.strategy_version_id, for_update=True
        )
        if version is None:
            raise _not_found()
        if version.status == "PUBLISHED" and run.status == "SUCCEEDED":
            return FrozenPublication(version=version, run=run, replayed=True)
        if version.status not in {"PUBLISHING", "PUBLISH_FAILED"} or run.status not in {
            "PENDING",
            "RUNNING",
            "FAILED",
        }:
            raise AppError(
                code="STRATEGY_PUBLISH_FAILED",
                message="发布任务状态不允许执行",
                status_code=409,
            )
        if version.status == "PUBLISH_FAILED":
            version.status = "PUBLISHING"
            await self._repository.set_strategy_status(
                version.strategy_id, "PUBLISHING"
            )
        run.status = "RUNNING"
        await self._repository.set_strategy_run_status(run.id, "RUNNING")
        return FrozenPublication(version=version, run=run)

    async def complete_publish_run(
        self,
        run_id: UUID,
        *,
        git_commit: str,
        context: StrategyCommandContext,
    ) -> StrategyVersion:
        run = await self._repository.get_strategy_run(run_id, for_update=True)
        if run is None:
            raise _not_found()
        version = await self._repository.get_version_by_id(
            run.strategy_version_id, for_update=True
        )
        if version is None:
            raise _not_found()
        if run.status == "SUCCEEDED":
            if version.status != "PUBLISHED" or version.git_commit != git_commit:
                raise _idempotency_conflict()
            return version
        if run.status != "RUNNING":
            raise AppError(
                code="STRATEGY_PUBLISH_FAILED",
                message="发布任务尚未领取",
                status_code=409,
            )
        published = await self.complete_publish(
            version.strategy_id,
            version.id,
            git_commit=git_commit,
            context=context,
        )
        run.status = "SUCCEEDED"
        await self._repository.set_strategy_run_status(run.id, "SUCCEEDED")
        return published

    async def fail_publish_run(
        self,
        run_id: UUID,
        error_code: str,
        *,
        context: StrategyCommandContext,
    ) -> StrategyVersion:
        run = await self._repository.get_strategy_run(run_id, for_update=True)
        if run is None:
            raise _not_found()
        version = await self._repository.get_version_by_id(
            run.strategy_version_id, for_update=True
        )
        if version is None:
            raise _not_found()
        if version.status == "PUBLISHED":
            if version.git_commit is None:
                raise AppError(
                    code="STRATEGY_PUBLISH_STATE_UNCERTAIN",
                    message="已发布版本缺少 Git 提交标识，暂时不可绑定",
                    status_code=503,
                )
            run.status = "SUCCEEDED"
            await self._repository.set_strategy_run_status(run.id, "SUCCEEDED")
            return version
        if run.status == "FAILED":
            replay = await self._audit.find_by_idempotency(
                _audit_key(
                    "strategy.publish_failed",
                    version.strategy_id,
                    context.idempotency_key,
                )
            )
            if (
                replay is None
                or (replay.after_summary or {}).get("error_code") != error_code
                or (replay.after_summary or {}).get("run_id") != str(run.id)
            ):
                raise _idempotency_conflict()
            return version
        failed = await self.fail_publish(
            version.strategy_id,
            version.id,
            error_code,
            context=context,
            run_id=run.id,
        )
        run.status = "FAILED"
        await self._repository.set_strategy_run_status(run.id, "FAILED")
        return failed

    async def fail_publish(
        self,
        strategy_id: UUID,
        version_id: UUID,
        error_code: str,
        *,
        context: StrategyCommandContext,
        run_id: UUID | None = None,
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
        if version.status not in {"PUBLISHING", "PUBLISH_FAILED"}:
            raise AppError(
                code="STRATEGY_PUBLISH_FAILED",
                message="策略发布状态不允许失败补偿",
                status_code=409,
            )
        before_status = str(version.status)
        version.status = "PUBLISH_FAILED"
        version.git_commit = None
        version.published_at = None
        await self._repository.set_strategy_status(strategy_id, "PUBLISH_FAILED")
        strategy.status = "PUBLISH_FAILED"
        await self._record(
            "strategy.publish_failed",
            strategy,
            context,
            before={
                "status": before_status,
                "version_id": str(version.id),
                "run_id": str(run_id) if run_id is not None else None,
            },
            after={
                "status": "PUBLISH_FAILED",
                "version_id": str(version.id),
                "run_id": str(run_id) if run_id is not None else None,
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
            replay = await self._audit.find_by_idempotency(
                _audit_key("strategy.archived", strategy.id, context.idempotency_key)
            )
            if (
                replay is None
                or (replay.before_summary or {}).get("draft_version")
                != expected_version
            ):
                raise _idempotency_conflict()
            return strategy
        if strategy.status != "PUBLISHED":
            raise AppError(
                code="STRATEGY_VERSION_IMMUTABLE",
                message="只有已发布策略可以归档",
                status_code=409,
            )
        draft = await self._repository.get_draft(strategy_id, for_update=True)
        if draft is None:
            raise _not_found()
        if draft.draft_version != expected_version:
            raise _version_conflict(draft)
        before_status = strategy.status
        version = await self._repository.latest_published_version(strategy_id)
        if version is None or version.status != "PUBLISHED":
            raise AppError(
                code="STRATEGY_PUBLISH_STATE_UNCERTAIN",
                message="已发布版本缺失，暂时不能归档",
                status_code=503,
            )
        version.status = "ARCHIVED"
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

    async def restore(
        self,
        strategy_id: UUID,
        *,
        expected_version: int,
        context: StrategyCommandContext,
    ) -> Strategy:
        _require_context(context)
        strategy = await self._locked_strategy(strategy_id)
        draft = await self._repository.get_draft(strategy_id, for_update=True)
        if draft is None:
            raise _not_found()
        if draft.draft_version != expected_version:
            raise _version_conflict(draft)
        if strategy.status == "PUBLISHED":
            replay = await self._audit.find_by_idempotency(
                _audit_key("strategy.restored", strategy.id, context.idempotency_key)
            )
            if (
                replay is None
                or (replay.before_summary or {}).get("draft_version")
                != expected_version
                or (replay.after_summary or {}).get("status") != "PUBLISHED"
            ):
                raise _idempotency_conflict()
            return strategy
        if strategy.status != "ARCHIVED":
            raise AppError(
                code="STRATEGY_VERSION_IMMUTABLE",
                message="只有已归档策略可以恢复",
                status_code=409,
            )
        version = await self._repository.latest_published_version(strategy_id)
        if version is None or version.status != "ARCHIVED":
            raise AppError(
                code="STRATEGY_PUBLISH_STATE_UNCERTAIN",
                message="归档版本缺失，暂时不能恢复",
                status_code=503,
            )
        version.status = "PUBLISHED"
        await self._repository.set_strategy_status(strategy_id, "PUBLISHED")
        strategy.status = "PUBLISHED"
        await self._record(
            "strategy.restored",
            strategy,
            context,
            before={"status": "ARCHIVED", "draft_version": draft.draft_version},
            after={"status": "PUBLISHED", "draft_version": draft.draft_version},
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
                payload={
                    "event_type": topic,
                    "strategy_id": str(strategy.id),
                    "request_id": context.request_id,
                    "actor_user_id": context.actor_user_id,
                    **after,
                },
            )
        )

    @staticmethod
    def hash_source(source_code: str) -> str:
        return hashlib.sha256(source_code.encode()).hexdigest()

    @staticmethod
    def hash_snapshot(snapshot: dict[str, Any]) -> str:
        return _json_hash(_validated_release_snapshot(snapshot))


def _same_release(version: StrategyVersion, release: dict[str, Any]) -> bool:
    return (
        version.strategy_metadata == release["metadata"]
        and version.parameter_schema == release["parameter_schema"]
        and version.environment_version == release["environment_version"]
        and version.runner_image_digest == release["runner_image_digest"]
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


def _json_hash(value: dict[str, Any]) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def _validate_environment(environment_version: str, runner_image_digest: str) -> None:
    if (
        not environment_version.strip()
        or re.fullmatch(r"sha256:[0-9a-f]{64}", runner_image_digest) is None
    ):
        raise _invalid("策略运行环境或镜像摘要无效")


def _validated_release_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    value = _json_snapshot(snapshot)
    required = {
        "schema_version",
        "source_code_hash",
        "metadata",
        "metadata_hash",
        "parameter_schema",
        "parameter_schema_hash",
        "params",
        "parameter_hash",
        "environment_version",
        "environment_hash",
        "runner_image_digest",
        "checks",
    }
    if set(value) != required:
        raise _invalid("验证证据缺少发布所需的冻结事实")
    if (
        value["metadata_hash"] != _json_hash(value["metadata"])
        or value["parameter_schema_hash"] != _json_hash(value["parameter_schema"])
        or value["parameter_hash"] != _json_hash(value["params"])
        or value["environment_hash"]
        != hashlib.sha256(value["environment_version"].encode()).hexdigest()
    ):
        raise AppError(
            code="STRATEGY_VALIDATION_STALE",
            message="验证证据不完整或内容哈希不一致",
            status_code=409,
        )
    _validate_environment(value["environment_version"], value["runner_image_digest"])
    _validate_completed_checks(value)
    return value


def _completed_validation_snapshot(
    run: StrategyValidationRun,
    *,
    succeeded: bool,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    checks = _json_snapshot(evidence)
    base = _json_snapshot(run.evidence_snapshot)
    if set(base) != {
        "schema_version",
        "source_code_hash",
        "metadata",
        "metadata_hash",
        "parameter_schema",
        "parameter_schema_hash",
        "params",
        "parameter_hash",
        "environment_version",
        "environment_hash",
        "runner_image_digest",
        "checks",
    }:
        raise _invalid("验证请求快照结构无效")
    result = {**base, "checks": checks}
    if succeeded:
        _validated_release_snapshot(result)
    return result


def _validate_completed_checks(snapshot: dict[str, Any]) -> None:
    checks = snapshot["checks"]
    if not isinstance(checks, dict) or set(checks) != {
        "static_analysis",
        "fixed_sample",
        "specified_stock",
        "holdout_backtest",
    }:
        raise AppError(
            code="STRATEGY_VALIDATION_STALE",
            message="验证证据必须包含四级验证事实",
            status_code=409,
        )
    common = {
        "run_id",
        "task_id",
        "snapshot_id",
        "status",
        "source_code_hash",
        "metadata_hash",
        "parameter_schema_hash",
        "parameter_hash",
        "environment_hash",
        "runner_image_digest",
    }
    train = {"training_start", "training_end", "training_data_hash"}
    expected = {
        "static_analysis": common,
        "fixed_sample": common | train,
        "specified_stock": common | train | {"security_id"},
        "holdout_backtest": common
        | train
        | {"security_id", "test_start", "test_end", "test_data_hash"},
    }
    for name, required in expected.items():
        value = checks[name]
        if not isinstance(value, dict) or set(value) != required:
            raise _stale_evidence(f"{name} 证据结构无效")
        try:
            UUID(str(value["run_id"]))
            UUID(str(value["task_id"]))
            UUID(str(value["snapshot_id"]))
        except (TypeError, ValueError) as exc:
            raise _stale_evidence(f"{name} 运行标识无效") from exc
        if value["status"] != "SUCCEEDED":
            raise _stale_evidence(f"{name} 未成功")
        for key in (
            "source_code_hash",
            "metadata_hash",
            "parameter_schema_hash",
            "parameter_hash",
            "environment_hash",
        ):
            if value[key] != snapshot[key]:
                raise _stale_evidence(f"{name} 未绑定当前发布事实")
        if value["runner_image_digest"] != snapshot["runner_image_digest"]:
            raise _stale_evidence(f"{name} 未绑定当前镜像")
        if "training_start" in value:
            start = _evidence_date(value["training_start"], name)
            end = _evidence_date(value["training_end"], name)
            if start > end or not _sha256(value["training_data_hash"]):
                raise _stale_evidence(f"{name} 训练数据证据无效")
        if name == "holdout_backtest":
            test_start = _evidence_date(value["test_start"], name)
            test_end = _evidence_date(value["test_end"], name)
            if (
                end >= test_start
                or test_start > test_end
                or not _sha256(value["test_data_hash"])
            ):
                raise _stale_evidence("样本外回测日期或数据哈希无效")


def _evidence_date(value: Any, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise _stale_evidence(f"{name} 日期无效") from exc


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _stale_evidence(message: str) -> AppError:
    return AppError(
        code="STRATEGY_VALIDATION_STALE",
        message=message,
        status_code=409,
    )


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
