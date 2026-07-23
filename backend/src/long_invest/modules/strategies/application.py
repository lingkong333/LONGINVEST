from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from long_invest.modules.strategies.contracts import (
    StrategyDraftView,
    StrategyForecastRequest,
    StrategyLifecycleStatus,
    StrategyOperationBatchResult,
    StrategyOperationItemResult,
    StrategyOperationItemStatus,
    StrategyStockTestPort,
    StrategyStockTestRequest,
    StrategyStockTestSubmission,
    StrategySubscriptionScope,
    StrategySubscriptionScopePort,
    StrategyVersionOperation,
    StrategyVersionTargetPort,
    StrategyVersionTargetRequest,
    StrategyVersionView,
    ValidationEvidenceClaim,
    ValidationEvidenceVerifier,
)
from long_invest.modules.strategies.forecast import hash_source_code
from long_invest.modules.strategies.git_store import StrategyGitStore
from long_invest.modules.strategies.outbox import StrategyOutboxAdapter
from long_invest.modules.strategies.repository import StrategyRepository
from long_invest.modules.strategies.service import (
    PublishEvidence,
    StrategyCommandContext,
    StrategyService,
)
from long_invest.modules.strategies.static_analysis import analyze_strategy_source
from long_invest.platform.audit.service import AuditService
from long_invest.platform.config.settings import get_settings
from long_invest.platform.database.engine import Database, get_database
from long_invest.platform.errors import AppError
from long_invest.platform.json_snapshot import thaw_json_value


class UnconfiguredValidationEvidenceVerifier:
    async def verify(self, claim: ValidationEvidenceClaim) -> bool:
        return False


class ConfiguredValidationEvidenceVerifier:
    async def verify(self, claim: ValidationEvidenceClaim) -> bool:
        return await get_configured_validation_evidence_verifier().verify(claim)


_validation_evidence_verifier_factory: (
    Callable[[], ValidationEvidenceVerifier] | None
) = None
_operation_ports_factory: (
    Callable[
        [],
        tuple[
            StrategyStockTestPort,
            StrategySubscriptionScopePort,
            StrategyVersionTargetPort,
        ],
    ]
    | None
) = None


def configure_strategy_validation_evidence_verifier(
    factory: Callable[[], ValidationEvidenceVerifier],
) -> None:
    global _validation_evidence_verifier_factory
    _validation_evidence_verifier_factory = factory


def configure_strategy_operation_ports(
    factory: Callable[
        [],
        tuple[
            StrategyStockTestPort,
            StrategySubscriptionScopePort,
            StrategyVersionTargetPort,
        ],
    ],
) -> None:
    global _operation_ports_factory
    _operation_ports_factory = factory


def get_configured_validation_evidence_verifier() -> ValidationEvidenceVerifier:
    if _validation_evidence_verifier_factory is None:
        return UnconfiguredValidationEvidenceVerifier()
    return _validation_evidence_verifier_factory()


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
        evidence_verifier: ValidationEvidenceVerifier,
        stock_tests: StrategyStockTestPort | None = None,
        subscription_scope: StrategySubscriptionScopePort | None = None,
        version_targets: StrategyVersionTargetPort | None = None,
    ) -> None:
        self._database = database
        self._git = git_store
        self._repository_factory = repository_factory
        self._audit_factory = audit_factory
        self._event_factory = event_factory
        self._service_factory = service_factory
        self._environment_version = environment_version
        self._runner_image_digest = runner_image_digest
        self._evidence_verifier = evidence_verifier
        self._stock_tests = stock_tests
        self._subscription_scope = subscription_scope
        self._version_targets = version_targets

    async def list(self, **kwargs: Any):
        return await self._read("list", **kwargs)

    async def get(self, strategy_id: UUID):
        return await self._read("get", strategy_id)

    async def get_draft(self, strategy_id: UUID):
        return await self._read("get_draft", strategy_id)

    async def get_draft_by_id(self, draft_id: UUID):
        return await self._read("get_draft_by_id", draft_id)

    async def get_validation_run(self, validation_run_id: UUID):
        return await self._read("get_validation_run", validation_run_id)

    async def list_revisions(self, strategy_id: UUID, **kwargs: Any):
        return await self._read("list_revisions", strategy_id, **kwargs)

    async def list_versions(self, strategy_id: UUID, **kwargs: Any):
        return await self._read("list_versions", strategy_id, **kwargs)

    async def get_execution_snapshot(
        self, strategy_version_id: UUID
    ) -> StrategyVersionView | None:
        version = await self._read("get_published_version_by_id", strategy_version_id)
        if version is None:
            return None
        return StrategyVersionView(
            id=version.id,
            strategy_id=version.strategy_id,
            version_no=version.version_no,
            source_code=version.source_code,
            metadata=version.strategy_metadata,
            parameter_schema=version.parameter_schema,
            environment_version=version.environment_version,
            runner_image_digest=version.runner_image_digest,
            source_code_hash=version.source_code_hash,
            git_commit=version.git_commit,
            validation_run_id=version.validation_run_id,
            status=StrategyLifecycleStatus(version.status),
            published_at=version.published_at,
            created_at=version.created_at,
        )

    async def published_version(self, strategy_version_id: UUID) -> bool:
        snapshot = await self.get_execution_snapshot(strategy_version_id)
        return (
            snapshot is not None
            and snapshot.status is StrategyLifecycleStatus.PUBLISHED
        )

    async def verify_forecast_request(self, request: StrategyForecastRequest) -> bool:
        if request.strategy_version_id is not None:
            snapshot = await self.get_execution_snapshot(request.strategy_version_id)
            return snapshot is not None and all(
                (
                    request.strategy_id == snapshot.strategy_id,
                    request.source_code == snapshot.source_code,
                    request.source_code_hash == snapshot.source_code_hash,
                    thaw_json_value(request.metadata)
                    == thaw_json_value(snapshot.metadata),
                    thaw_json_value(request.parameter_schema)
                    == thaw_json_value(snapshot.parameter_schema),
                    request.environment_version == snapshot.environment_version,
                    request.runner_image_digest == snapshot.runner_image_digest,
                )
            )
        if request.draft_id is None or request.draft_version is None:
            return False
        draft = await self.get_draft(request.strategy_id)
        return all(
            (
                draft.id == request.draft_id,
                draft.draft_version == request.draft_version,
                draft.source_code == request.source_code,
                hash_source_code(draft.source_code) == request.source_code_hash,
                thaw_json_value(request.metadata) == draft.strategy_metadata,
                thaw_json_value(request.parameter_schema)
                == draft.parameter_schema,
                request.environment_version == self._environment_version,
                request.runner_image_digest == self._runner_image_digest,
            )
        )

    async def diff(self, strategy_id: UUID, *, revision_id: UUID):
        return await self._read("diff", strategy_id, revision_id=revision_id)

    async def test_stock(
        self,
        request: StrategyStockTestRequest,
        *,
        idempotency_key: str,
        request_id: str,
        actor_user_id: str,
        reason: str,
    ) -> StrategyStockTestSubmission:
        stock_tests = self._required_operation_port(self._stock_tests)
        draft = await self.get_draft(request.strategy_id)
        try:
            analyze_strategy_source(draft.source_code)
        except ValueError as exc:
            raise AppError(
                code=str(getattr(exc, "code", "STRATEGY_INPUT_INVALID")),
                message="策略草稿未通过语法和契约检查",
                status_code=422,
            ) from exc
        task_id = uuid5(
            NAMESPACE_URL,
            f"longinvest:strategy-test:{request.strategy_id}:{idempotency_key}",
        )
        return await stock_tests.submit_strategy_test(
            task_id=task_id,
            draft=StrategyDraftView(
                id=draft.id,
                strategy_id=draft.strategy_id,
                draft_version=draft.draft_version,
                source_code=draft.source_code,
                metadata=draft.strategy_metadata,
                parameter_schema=draft.parameter_schema,
            ),
            metadata=draft.strategy_metadata,
            parameter_schema=draft.parameter_schema,
            request=request,
            idempotency_key=idempotency_key,
            request_id=request_id,
            actor_user_id=actor_user_id,
            reason=reason,
        )

    async def apply_version(
        self,
        strategy_id: UUID,
        strategy_version_id: UUID,
        **kwargs: Any,
    ) -> StrategyOperationBatchResult:
        return await self._operate_version(
            strategy_id,
            strategy_version_id,
            operation=StrategyVersionOperation.APPLY,
            **kwargs,
        )

    async def rollback_version(
        self,
        strategy_id: UUID,
        strategy_version_id: UUID,
        **kwargs: Any,
    ) -> StrategyOperationBatchResult:
        return await self._operate_version(
            strategy_id,
            strategy_version_id,
            operation=StrategyVersionOperation.ROLLBACK,
            **kwargs,
        )

    async def _operate_version(
        self,
        strategy_id: UUID,
        strategy_version_id: UUID,
        *,
        operation: StrategyVersionOperation,
        scope: StrategySubscriptionScope,
        subscription_ids: tuple[UUID, ...],
        target_date: date,
        training_start_date: date,
        training_end_date: date,
        reason: str,
        idempotency_key: str,
        request_id: str,
        actor_user_id: str,
        session_id: str,
        trusted_ip: str,
    ) -> StrategyOperationBatchResult:
        subscriptions = self._required_operation_port(self._subscription_scope)
        version_targets = self._required_operation_port(self._version_targets)
        if scope is StrategySubscriptionScope.SELECTED and not subscription_ids:
            raise AppError(
                code="STRATEGY_SUBSCRIPTION_SCOPE_INVALID",
                message="选择指定范围时至少需要一个订阅",
                status_code=422,
            )
        if len(set(subscription_ids)) != len(subscription_ids):
            raise AppError(
                code="STRATEGY_SUBSCRIPTION_SCOPE_INVALID",
                message="订阅列表不能包含重复项",
                status_code=422,
            )
        candidates = await subscriptions.resolve_strategy_subscriptions(
            strategy_id=strategy_id,
            scope=scope,
            subscription_ids=subscription_ids,
        )
        resolved_subscription_ids = (
            subscription_ids
            if scope is StrategySubscriptionScope.SELECTED
            else tuple(item.subscription_id for item in candidates)
        )
        frozen = await self._write(
            "request_version_operation",
            strategy_id,
            strategy_version_id=strategy_version_id,
            operation=operation,
            scope=scope,
            subscription_ids=resolved_subscription_ids,
            target_date=target_date,
            training_start_date=training_start_date,
            training_end_date=training_end_date,
            context=self._context(
                {
                    "reason": reason,
                    "idempotency_key": idempotency_key,
                    "request_id": request_id,
                    "actor_user_id": actor_user_id,
                    "session_id": session_id,
                    "trusted_ip": trusted_ip,
                }
            ),
            conflict_code="STRATEGY_VERSION_OPERATION_CONFLICT",
        )
        if resolved_subscription_ids != frozen.subscription_ids:
            candidates = await subscriptions.resolve_strategy_subscriptions(
                strategy_id=strategy_id,
                scope=StrategySubscriptionScope.SELECTED,
                subscription_ids=frozen.subscription_ids,
            )
        candidate_by_id = {item.subscription_id: item for item in candidates}
        results: list[StrategyOperationItemResult] = []
        for subscription_id in frozen.subscription_ids:
            candidate = candidate_by_id.get(subscription_id)
            if candidate is None:
                results.append(
                    StrategyOperationItemResult(
                        subscription_id=subscription_id,
                        status=StrategyOperationItemStatus.REJECTED,
                        code="STRATEGY_SUBSCRIPTION_NOT_FOUND",
                    )
                )
                continue
            item_key = f"{idempotency_key}:{operation.value.lower()}:{subscription_id}"
            try:
                submission = await version_targets.submit_strategy_version_target(
                    StrategyVersionTargetRequest(
                        operation=operation,
                        strategy_id=strategy_id,
                        strategy_version_id=frozen.strategy_version_id,
                        subscription_id=subscription_id,
                        subscription_version=candidate.subscription_version,
                        target_version=candidate.target_version,
                        parameter_snapshot=candidate.parameter_snapshot,
                        target_date=target_date,
                        training_start_date=training_start_date,
                        training_end_date=training_end_date,
                        reason=reason,
                        idempotency_key=item_key,
                        request_id=request_id,
                        actor_user_id=actor_user_id,
                        session_id=session_id,
                        trusted_ip=trusted_ip,
                    )
                )
            except AppError as exc:
                results.append(
                    StrategyOperationItemResult(
                        subscription_id=subscription_id,
                        status=StrategyOperationItemStatus.REJECTED,
                        code=exc.code,
                    )
                )
            except (TimeoutError, RuntimeError):
                results.append(
                    StrategyOperationItemResult(
                        subscription_id=subscription_id,
                        status=StrategyOperationItemStatus.FAILED,
                        code="STRATEGY_TARGET_SUBMISSION_FAILED",
                    )
                )
            else:
                results.append(
                    StrategyOperationItemResult(
                        subscription_id=subscription_id,
                        status=(
                            StrategyOperationItemStatus.REUSED
                            if submission.replayed
                            else StrategyOperationItemStatus.ACCEPTED
                        ),
                        code=submission.code,
                        run_id=submission.run_id,
                        job_id=submission.job_id,
                    )
                )
        return StrategyOperationBatchResult(
            operation=operation,
            strategy_id=strategy_id,
            strategy_version_id=frozen.strategy_version_id,
            replayed=frozen.replayed,
            items=tuple(results),
        )

    @staticmethod
    def _required_operation_port(value: Any) -> Any:
        if value is None:
            raise AppError(
                code="STRATEGY_CAPABILITY_NOT_READY",
                message="策略操作依赖尚未接入",
                status_code=503,
            )
        return value

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
        metadata: dict[str, Any] | None = None,
        parameter_schema: dict[str, Any] | None = None,
        **context: str,
    ):
        return await self._write(
            "save_draft",
            strategy_id,
            source_code=source_code,
            metadata=metadata,
            parameter_schema=parameter_schema,
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
        backtest_task_id: UUID,
        params: dict[str, Any],
        **context: str,
    ):
        return await self._write(
            "request_validation",
            strategy_id,
            backtest_task_id=backtest_task_id,
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
        validation = await self._read("get_validation_evidence", validation_run_id)
        snapshot = validation.evidence_snapshot
        evidence_hash = StrategyService.hash_snapshot(snapshot)
        claim = ValidationEvidenceClaim(
            validation_run_id=validation.id,
            strategy_id=validation.strategy_id,
            draft_version=validation.draft_version,
            source_code_hash=validation.source_code_hash,
            metadata_hash=str(snapshot["metadata_hash"]),
            parameter_schema_hash=str(snapshot["parameter_schema_hash"]),
            parameter_hash=str(snapshot["parameter_hash"]),
            environment_hash=str(snapshot["environment_hash"]),
            runner_image_digest=str(snapshot["runner_image_digest"]),
            checks=dict(snapshot["checks"]),
        )
        if not await self._evidence_verifier.verify(claim):
            raise AppError(
                code="STRATEGY_VALIDATION_STALE",
                message="发布前无法核实回测和验证事实",
                status_code=409,
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
    database = get_database()
    operation_ports = _operation_ports_factory() if _operation_ports_factory else None
    return StrategyApplication(
        database,
        git_store=StrategyGitStore(root),
        environment_version=getattr(
            settings, "strategy_environment_version", "python-3.12"
        ),
        runner_image_digest=getattr(
            settings,
            "strategy_runner_image_digest",
            "",
        ),
        evidence_verifier=ConfiguredValidationEvidenceVerifier(),
        stock_tests=operation_ports[0] if operation_ports else None,
        subscription_scope=operation_ports[1] if operation_ports else None,
        version_targets=operation_ports[2] if operation_ports else None,
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
