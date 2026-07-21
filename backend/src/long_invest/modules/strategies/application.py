from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from long_invest.modules.strategies.git_store import StrategyGitStore
from long_invest.modules.strategies.outbox import StrategyOutboxAdapter
from long_invest.modules.strategies.repository import StrategyRepository
from long_invest.modules.strategies.service import (
    PublishEvidence,
    StrategyCommandContext,
    StrategyService,
)
from long_invest.platform.audit.service import AuditService
from long_invest.platform.config.settings import get_settings
from long_invest.platform.database.engine import Database, get_database
from long_invest.platform.errors import AppError


class ValidationEvidenceVerifier(Protocol):
    async def verify(self, evidence_snapshot: dict[str, Any]) -> bool: ...


class FailClosedEvidenceVerifier:
    async def verify(self, evidence_snapshot: dict[str, Any]) -> bool:
        return False


class StrategyApplication:
    def __init__(
        self,
        database: Database,
        *,
        git_store: StrategyGitStore,
        repository_factory: Callable[..., Any] = StrategyRepository,
        audit_factory: Callable[..., Any] = AuditService,
        event_factory: Callable[..., Any] = StrategyOutboxAdapter,
        service_factory: Callable[..., Any] = StrategyService,
        environment_version: str = "python-3.12",
        runner_image_digest: str = "",
        evidence_verifier: ValidationEvidenceVerifier | None = None,
    ) -> None:
        self._database = database
        self._git = git_store
        self._repository_factory = repository_factory
        self._audit_factory = audit_factory
        self._event_factory = event_factory
        self._service_factory = service_factory
        self._environment_version = environment_version
        self._runner_image_digest = runner_image_digest
        self._evidence_verifier = evidence_verifier or FailClosedEvidenceVerifier()

    async def list(self, **kwargs: Any):
        return await self._read("list", **kwargs)

    async def get(self, strategy_id: UUID):
        return await self._read("get", strategy_id)

    async def get_draft(self, strategy_id: UUID):
        return await self._read("get_draft", strategy_id)

    async def list_revisions(self, strategy_id: UUID, **kwargs: Any):
        return await self._read("list_revisions", strategy_id, **kwargs)

    async def list_versions(self, strategy_id: UUID, **kwargs: Any):
        return await self._read("list_versions", strategy_id, **kwargs)

    async def diff(self, strategy_id: UUID, *, revision_id: UUID):
        return await self._read("diff", strategy_id, revision_id=revision_id)

    async def create(self, *, name: str, **context: str):
        return await self._write(
            "create", name, self._context(context), conflict_code="STRATEGY_CONFLICT"
        )

    async def save_draft(
        self,
        strategy_id: UUID,
        *,
        source_code: str,
        expected_version: int,
        create_revision: bool,
        **context: str,
    ):
        return await self._write(
            "save_draft",
            strategy_id,
            source_code=source_code,
            expected_version=expected_version,
            create_revision=create_revision,
            context=self._context(context),
            conflict_code="STRATEGY_VERSION_CONFLICT",
        )

    async def rename(
        self,
        strategy_id: UUID,
        *,
        name: str,
        expected_version: int,
        **context: str,
    ):
        return await self._write(
            "rename",
            strategy_id,
            name=name,
            expected_version=expected_version,
            context=self._context(context),
            conflict_code="STRATEGY_VERSION_CONFLICT",
        )

    async def restore_revision(
        self,
        strategy_id: UUID,
        *,
        revision_id: UUID,
        expected_version: int,
        **context: str,
    ):
        return await self._write(
            "restore_revision",
            strategy_id,
            revision_id=revision_id,
            expected_version=expected_version,
            context=self._context(context),
            conflict_code="STRATEGY_VERSION_CONFLICT",
        )

    async def request_validation(
        self,
        strategy_id: UUID,
        *,
        metadata: dict[str, Any],
        parameter_schema: dict[str, Any],
        params: dict[str, Any],
        **context: str,
    ):
        return await self._write(
            "request_validation",
            strategy_id,
            metadata=metadata,
            parameter_schema=parameter_schema,
            params=params,
            environment_version=self._environment_version,
            runner_image_digest=self._runner_image_digest,
            context=self._context(context),
            conflict_code="STRATEGY_VALIDATION_CONFLICT",
        )

    async def record_validation_result_from_worker(
        self,
        validation_run_id: UUID,
        *,
        succeeded: bool,
        error_code: str | None,
        evidence_snapshot: dict[str, Any],
        **context: str,
    ):
        return await self._write(
            "complete_validation",
            validation_run_id,
            succeeded=succeeded,
            error_code=error_code,
            evidence_snapshot=evidence_snapshot,
            context=self._context(context),
            conflict_code="STRATEGY_VALIDATION_CONFLICT",
        )

    async def archive(
        self, strategy_id: UUID, *, expected_version: int, **context: str
    ):
        return await self._write(
            "archive",
            strategy_id,
            expected_version=expected_version,
            context=self._context(context),
            conflict_code="STRATEGY_VERSION_CONFLICT",
        )

    async def restore(
        self, strategy_id: UUID, *, expected_version: int, **context: str
    ):
        return await self._write(
            "restore",
            strategy_id,
            expected_version=expected_version,
            context=self._context(context),
            conflict_code="STRATEGY_VERSION_CONFLICT",
        )

    async def publish(
        self,
        *,
        strategy_id: UUID,
        validation_run_id: UUID,
        expected_draft_version: int,
        **context: str,
    ):
        command_context = self._context(context)
        validation = await self._read(
            "get_validation_evidence", validation_run_id
        )
        if not await self._evidence_verifier.verify(validation.evidence_snapshot):
            raise AppError(
                code="STRATEGY_VALIDATION_STALE",
                message="发布前无法核实回测和验证事实",
                status_code=409,
            )
        evidence_hash = StrategyService.hash_snapshot(
            validation.evidence_snapshot
        )
        evidence = PublishEvidence(
            validation_run_id=validation_run_id,
            expected_draft_version=expected_draft_version,
            evidence_hash=evidence_hash,
        )
        return await self._write(
            "begin_publish",
            strategy_id,
            evidence,
            command_context,
            conflict_code="STRATEGY_PUBLISH_FAILED",
        )

    async def execute_publish(self, run_id: UUID):
        frozen = await self._write(
            "claim_publish_run",
            run_id,
            conflict_code="STRATEGY_PUBLISH_FAILED",
        )
        version = frozen.version
        if frozen.replayed and version.status == "PUBLISHED":
            return version
        command_context = self._worker_context(run_id)
        try:
            git_commit = self._git.publish(
                strategy_id=str(version.strategy_id),
                version_no=version.version_no,
                source_code=version.source_code,
                source_code_hash=version.source_code_hash,
                manifest={
                    "metadata": version.strategy_metadata,
                    "parameter_schema": version.parameter_schema,
                    "environment_version": version.environment_version,
                    "runner_image_digest": version.runner_image_digest,
                    "validation_run_id": str(version.validation_run_id),
                },
            )
            if not self._git.verify_source(
                strategy_id=str(version.strategy_id),
                version_no=version.version_no,
                commit=git_commit,
                source_code_hash=version.source_code_hash,
            ):
                raise RuntimeError("published Git content does not match snapshot")
        except Exception as exc:
            await self._mark_publish_failed_run(
                run_id,
                "STRATEGY_GIT_FAILED",
                context=command_context,
            )
            raise _publish_failed() from exc
        try:
            return await self._write(
                "complete_publish_run",
                run_id,
                git_commit=git_commit,
                context=command_context,
                conflict_code="STRATEGY_PUBLISH_FAILED",
            )
        except Exception as exc:
            await self._mark_publish_failed_run(
                run_id,
                "STRATEGY_DATABASE_FAILED",
                context=command_context,
            )
            if isinstance(exc, AppError):
                raise
            raise _publish_failed() from exc

    async def recover_publish_runs(self) -> list[dict[str, Any]]:
        runs = await self._read("list_recoverable_publish_runs")
        results: list[dict[str, Any]] = []
        for run in runs:
            try:
                version = await self.execute_publish(run.id)
                results.append(
                    {
                        "run_id": str(run.id),
                        "status": "SUCCEEDED",
                        "version_id": str(version.id),
                    }
                )
            except AppError as exc:
                results.append(
                    {
                        "run_id": str(run.id),
                        "status": "FAILED",
                        "error_code": exc.code,
                    }
                )
        return results

    async def _mark_publish_failed_run(
        self,
        run_id: UUID,
        error_code: str,
        *,
        context: StrategyCommandContext,
    ) -> None:
        try:
            await self._write(
                "fail_publish_run",
                run_id,
                error_code,
                context=context,
                conflict_code="STRATEGY_PUBLISH_FAILED",
            )
        except Exception as exc:
            raise AppError(
                code="STRATEGY_PUBLISH_STATE_UNCERTAIN",
                message="发布失败状态暂时无法确认，请勿绑定该版本",
                status_code=503,
            ) from exc

    async def _read(self, method: str, *args: Any, **kwargs: Any):
        try:
            async with self._database.session() as session:
                return await getattr(self._service(session), method)(*args, **kwargs)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def _write(
        self,
        method: str,
        *args: Any,
        conflict_code: str,
        **kwargs: Any,
    ):
        try:
            async with self._database.transaction() as session:
                return await getattr(self._service(session), method)(*args, **kwargs)
        except AppError:
            raise
        except IntegrityError as exc:
            raise AppError(
                code=conflict_code,
                message="策略请求与已有操作冲突",
                status_code=409,
            ) from exc
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    def _service(self, session: Any) -> StrategyService:
        return self._service_factory(
            self._repository_factory(session),
            audit=self._audit_factory(session),
            events=self._event_factory(session),
        )

    @staticmethod
    def _context(values: dict[str, str]) -> StrategyCommandContext:
        return StrategyCommandContext(
            request_id=values.get("request_id", ""),
            idempotency_key=values.get("idempotency_key", ""),
            actor_user_id=values.get("actor_user_id", ""),
            session_id=values.get("session_id", ""),
            trusted_ip=values.get("trusted_ip", ""),
            reason=values.get("reason", ""),
        )

    @staticmethod
    def _worker_context(run_id: UUID) -> StrategyCommandContext:
        return StrategyCommandContext(
            request_id=f"strategy-publish:{run_id}",
            idempotency_key=f"strategy-publish:{run_id}",
            actor_user_id="system:strategy-worker",
            session_id="system:strategy-worker",
            trusted_ip="127.0.0.1",
            reason="执行已确认的策略发布任务",
        )


def get_strategy_application() -> StrategyApplication:
    settings = get_settings()
    root = Path(
        getattr(settings, "strategy_git_path", "/var/lib/long-invest/strategies")
    )
    return StrategyApplication(
        get_database(),
        git_store=StrategyGitStore(root),
        environment_version=getattr(
            settings, "strategy_environment_version", "python-3.12"
        ),
        runner_image_digest=getattr(
            settings,
            "strategy_runner_image_digest",
            "",
        ),
    )


def _publish_failed() -> AppError:
    return AppError(
        code="STRATEGY_PUBLISH_FAILED",
        message="策略发布失败，冻结版本已保留，可安全重试",
        status_code=503,
    )


def _backend_unavailable() -> AppError:
    return AppError(
        code="STRATEGY_BACKEND_UNAVAILABLE",
        message="策略服务暂时不可用",
        status_code=503,
    )
