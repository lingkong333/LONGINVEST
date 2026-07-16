from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from long_invest.modules.market_data.contracts import (
    OpenQualityIssue,
    QualityIssueStatus,
    QualityResolutionAction,
    QualitySeverity,
    ResolveQualityIssue,
)
from long_invest.modules.market_data.integrations import (
    TransactionalQualityEventAdapter,
)
from long_invest.modules.market_data.models import DataQualityIssue
from long_invest.modules.market_data.repository import QualityIssueRepository
from long_invest.modules.market_data.service import QualityIssueService
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database
from long_invest.platform.outbox.models import EventOutbox


class _FailingWriter:
    async def append(self, **_kwargs) -> None:
        raise RuntimeError("forced outbox failure")


@pytest.mark.anyio
async def test_quality_resolution_and_outbox_are_atomic_and_idempotent() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    key = f"migration-quality-{uuid4()}"
    issue_id = None
    try:
        async with database.transaction() as session:
            result = await QualityIssueService(QualityIssueRepository(session)).open(
                OpenQualityIssue(
                    issue_type="QUOTE_CONFLICT",
                    subject_type="quote_cycle_item",
                    subject_id=str(uuid4()),
                    symbol="600000.SH",
                    severity=QualitySeverity.ERROR,
                    evidence={"sources": {"eastmoney": {"price": "10.00"}}},
                    dedupe_key=key,
                )
            )
            issue_id = result.issue.id

        command = ResolveQualityIssue(
            issue_id=issue_id,
            action=QualityResolutionAction.RESOLVE,
            actor_user_id="integration-test",
            reason="invalid provider fact",
        )
        with pytest.raises(RuntimeError, match="forced outbox failure"):
            async with database.transaction() as session:
                await QualityIssueService(
                    QualityIssueRepository(session),
                    events=TransactionalQualityEventAdapter(
                        session, writer=_FailingWriter()
                    ),
                ).resolve(command)

        async with database.session() as session:
            issue = await session.get(DataQualityIssue, issue_id)
            assert issue is not None
            assert issue.status == QualityIssueStatus.OPEN
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(EventOutbox)
                    .where(EventOutbox.aggregate_id == str(issue_id))
                )
                == 0
            )

        async with database.transaction() as session:
            result = await QualityIssueService(
                QualityIssueRepository(session),
                events=TransactionalQualityEventAdapter(session),
            ).resolve(command)
            assert result.replayed is False

        async with database.transaction() as session:
            replay = await QualityIssueService(
                QualityIssueRepository(session),
                events=TransactionalQualityEventAdapter(session),
            ).resolve(command)
            assert replay.replayed is True

        async with database.session() as session:
            issue = await session.get(DataQualityIssue, issue_id)
            assert issue is not None
            assert issue.status == QualityIssueStatus.RESOLVED
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(EventOutbox)
                    .where(
                        EventOutbox.aggregate_id == str(issue_id),
                        EventOutbox.topic == "data_quality_issue.resolved",
                        EventOutbox.payload["status"].as_string() == "RESOLVED",
                    )
                )
                == 1
            )
    finally:
        if issue_id is not None:
            async with database.transaction() as session:
                await session.execute(
                    delete(EventOutbox).where(
                        EventOutbox.aggregate_id == str(issue_id)
                    )
                )
                await session.execute(
                    delete(DataQualityIssue).where(DataQualityIssue.id == issue_id)
                )
        await database.dispose()
