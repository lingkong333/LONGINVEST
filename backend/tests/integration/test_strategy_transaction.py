from __future__ import annotations

import asyncio
import hashlib
import os
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from long_invest.modules.strategies.models import (
    Strategy,
    StrategyDraftRevision,
    StrategyValidationRun,
    StrategyVersion,
)
from long_invest.modules.strategies.outbox import StrategyOutboxAdapter
from long_invest.modules.strategies.repository import StrategyRepository
from long_invest.modules.strategies.service import (
    PublishEvidence,
    StrategyCommandContext,
    StrategyService,
)
from long_invest.platform.audit.models import AuditEvent
from long_invest.platform.audit.service import AuditService
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.models import Job
from long_invest.platform.outbox.models import EventOutbox

pytestmark = pytest.mark.skipif(
    os.environ.get("LONGINVEST_STRATEGY_TRANSACTION_TESTS") != "1",
    reason="requires migrated PostgreSQL profile",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def command_context(key: str) -> StrategyCommandContext:
    return StrategyCommandContext(
        request_id=f"request-{key}",
        idempotency_key=key,
        actor_user_id="strategy-integration-user",
        session_id="strategy-integration-session",
        trusted_ip="127.0.0.1",
        reason="策略事务集成测试",
    )


def transaction_service(session) -> StrategyService:
    return StrategyService(
        StrategyRepository(session),
        audit=AuditService(session),
        events=StrategyOutboxAdapter(session),
    )


def validation_checks(run: StrategyValidationRun) -> dict:
    snapshot = run.evidence_snapshot
    facts = {
        key: snapshot[key]
        for key in (
            "source_code_hash",
            "metadata_hash",
            "parameter_schema_hash",
            "parameter_hash",
            "environment_hash",
            "runner_image_digest",
        )
    }
    common = {
        "run_id": str(uuid4()),
        "task_id": str(uuid4()),
        "snapshot_id": str(uuid4()),
        "status": "SUCCEEDED",
        **facts,
    }
    training = {
        "training_start": "2010-01-01",
        "training_end": "2020-12-31",
        "training_data_hash": hashlib.sha256(b"training-bars").hexdigest(),
    }
    return {
        "static_analysis": dict(common),
        "fixed_sample": {**common, **training},
        "specified_stock": {
            **common,
            **training,
            "security_id": str(uuid4()),
        },
        "holdout_backtest": {
            **common,
            **training,
            "security_id": str(uuid4()),
            "test_start": "2021-01-01",
            "test_end": "2022-12-31",
            "test_data_hash": hashlib.sha256(b"test-bars").hexdigest(),
        },
    }


class FailingAudit:
    async def find_by_idempotency(self, _key):
        return None

    async def append(self, _record):
        raise RuntimeError("audit failed")


@pytest.mark.anyio
async def test_strategy_revision_validation_and_publish_binding_are_atomic() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    key = uuid4().hex
    try:
        async with database.transaction() as session:
            created = await transaction_service(session).create(
                f"策略-{key}", command_context(f"create-{key}")
            )
        async with database.transaction() as session:
            service = transaction_service(session)
            draft = await service.save_draft(
                created.strategy.id,
                source_code="def calculate_targets(history, params, context):\n"
                "    return {}\n",
                expected_version=1,
                create_revision=True,
                context=command_context(f"save-{key}"),
            )
            validation = await service.request_validation(
                created.strategy.id,
                metadata={"name": f"策略-{key}"},
                parameter_schema={"type": "object"},
                params={},
                environment_version="python-3.12",
                runner_image_digest="sha256:" + "a" * 64,
                context=command_context(f"validate-{key}"),
            )
        async with database.transaction() as session:
            await transaction_service(session).complete_validation(
                validation.id,
                succeeded=True,
                error_code=None,
                evidence_snapshot=validation_checks(validation),
                context=command_context(f"complete-{key}"),
            )
        async with database.transaction() as session:
            service = transaction_service(session)
            completed_validation = await service.get_validation_evidence(validation.id)
            frozen = await service.begin_publish(
                created.strategy.id,
                PublishEvidence(
                    validation_run_id=validation.id,
                    expected_draft_version=draft.draft_version,
                    evidence_hash=service.hash_snapshot(
                        completed_validation.evidence_snapshot
                    ),
                ),
                command_context(f"publish-{key}"),
            )
        async with database.transaction() as session:
            service = transaction_service(session)
            await service.claim_publish_run(frozen.run.id)
            await service.complete_publish_run(
                frozen.run.id,
                git_commit="a" * 40,
                context=command_context(f"publish-complete-{key}"),
            )
        async with database.transaction() as session:
            service = transaction_service(session)
            await service.archive(
                created.strategy.id,
                expected_version=draft.draft_version,
                context=command_context(f"archive-{key}"),
            )
            restored = await service.restore(
                created.strategy.id,
                expected_version=draft.draft_version,
                context=command_context(f"restore-{key}"),
            )

        async with database.session() as session:
            stored_validation = await session.get(
                StrategyValidationRun, validation.id
            )
            stored_version = await session.get(StrategyVersion, frozen.version.id)
            revision_count = await session.scalar(
                select(func.count())
                .select_from(StrategyDraftRevision)
                .where(StrategyDraftRevision.draft_id == draft.id)
            )
            audit_count = await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.object_id == str(created.strategy.id))
            )
            event_count = await session.scalar(
                select(func.count())
                .select_from(EventOutbox)
                .where(EventOutbox.aggregate_id == str(created.strategy.id))
            )
            jobs = list(
                (
                    await session.scalars(
                        select(Job).where(
                            Job.request_id.in_(
                                (
                                    f"request-validate-{key}",
                                    f"request-publish-{key}",
                                )
                            )
                        )
                    )
                ).all()
            )
            dispatch_count = await session.scalar(
                select(func.count())
                .select_from(EventOutbox)
                .where(
                    EventOutbox.topic == "jobs.dispatch",
                    EventOutbox.aggregate_id.in_([str(job.id) for job in jobs]),
                )
            )
        assert stored_validation is not None and stored_version is not None
        assert stored_validation.strategy_version_id == stored_version.id
        assert restored.status == "PUBLISHED"
        assert stored_version.status == "PUBLISHED"
        assert stored_version.strategy_metadata == stored_validation.evidence_snapshot[
            "metadata"
        ]
        assert revision_count == 1
        assert audit_count == event_count == 8
        assert {job.job_type for job in jobs} == {
            "STRATEGY_VALIDATE",
            "STRATEGY_PUBLISH",
        }
        assert {job.status for job in jobs} == {"PENDING_DISPATCH"}
        assert dispatch_count == 2
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_audit_failure_rolls_back_strategy_and_draft() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    key = uuid4().hex
    try:
        with pytest.raises(RuntimeError, match="audit failed"):
            async with database.transaction() as session:
                await StrategyService(
                    StrategyRepository(session),
                    audit=FailingAudit(),
                    events=StrategyOutboxAdapter(session),
                ).create(f"策略-{key}", command_context(f"create-{key}"))

        async with database.session() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(Strategy)
                .where(Strategy.name == f"策略-{key}")
            )
        assert count == 0
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_concurrent_draft_saves_allow_only_one_expected_version() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    key = uuid4().hex
    try:
        async with database.transaction() as session:
            created = await transaction_service(session).create(
                f"策略-{key}", command_context(f"create-{key}")
            )

        async def save(source_code: str, suffix: str):
            try:
                async with database.transaction() as session:
                    return await transaction_service(session).save_draft(
                        created.strategy.id,
                        source_code=source_code,
                        expected_version=1,
                        create_revision=False,
                        context=command_context(f"save-{key}-{suffix}"),
                    )
            except AppError as exc:
                return exc

        results = await asyncio.gather(save("first", "1"), save("second", "2"))

        assert sum(not isinstance(item, AppError) for item in results) == 1
        errors = [item for item in results if isinstance(item, AppError)]
        assert [error.code for error in errors] == ["STRATEGY_VERSION_CONFLICT"]
    finally:
        await database.dispose()
