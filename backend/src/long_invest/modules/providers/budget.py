from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, func, or_, select, text, update

from long_invest.modules.providers.contracts import ProviderCapability, ProviderCode
from long_invest.modules.providers.models import (
    ProviderBudgetPolicy,
    ProviderBudgetUsage,
    ProviderCapabilitySetting,
    ProviderRequestLease,
)
from long_invest.modules.providers.retry import ProviderHttpError

if TYPE_CHECKING:
    from long_invest.modules.providers.resilience import ProviderRouteSetting


@dataclass(frozen=True, slots=True)
class ProviderRequestContext:
    setting: ProviderRouteSetting


_request_context: ContextVar[ProviderRequestContext | None] = ContextVar(
    "provider_request_context", default=None
)


def enter_request_context(setting: ProviderRouteSetting) -> Token:
    return _request_context.set(ProviderRequestContext(setting))


def exit_request_context(token: Token) -> None:
    _request_context.reset(token)


@dataclass(frozen=True, slots=True)
class BudgetLease:
    token: str


class ProviderRequestBudget:
    """PostgreSQL-backed request budget claimed immediately before network I/O."""

    def __init__(self, database: Any) -> None:
        self._database = database

    @asynccontextmanager
    async def guard(self) -> AsyncIterator[None]:
        context = _request_context.get()
        if context is None:
            yield
            return
        deadline = monotonic() + context.setting.timeout_seconds
        while True:
            try:
                lease = await self.claim(context.setting)
                break
            except ProviderHttpError as error:
                if error.code not in {
                    "PROVIDER_MIN_INTERVAL_LIMITED",
                    "PROVIDER_TOTAL_CONCURRENCY_LIMITED",
                    "PROVIDER_CAPABILITY_CONCURRENCY_LIMITED",
                }:
                    raise
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise ProviderHttpError(
                        error.code, retryable=True
                    ) from error
                await asyncio.sleep(
                    min(error.retry_after_seconds or 0.05, remaining)
                )
        try:
            yield
        finally:
            await self.release(lease)

    async def claim(self, setting: ProviderRouteSetting) -> BudgetLease:
        denial: str | None = None
        retry_after_seconds: float | None = None
        token = uuid4().hex
        async with self._database.transaction() as session:
            now = await session.scalar(select(func.clock_timestamp()))
            if not isinstance(now, datetime):
                now = datetime.now(UTC)
            policy, capability_limit, min_interval = await self._configuration(
                session, setting
            )
            timezone = self._timezone(policy.reset_timezone)
            budget_date = now.astimezone(timezone).date()
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"provider-budget:{setting.provider.value}"},
            )
            locked_now = await session.scalar(select(func.clock_timestamp()))
            if isinstance(locked_now, datetime):
                now = locked_now
                budget_date = now.astimezone(timezone).date()
            await session.execute(
                delete(ProviderRequestLease).where(
                    ProviderRequestLease.provider_code == setting.provider.value,
                    or_(
                        ProviderRequestLease.released_at.is_not(None),
                        ProviderRequestLease.expires_at <= now,
                    ),
                )
            )
            usage = await session.scalar(
                select(ProviderBudgetUsage)
                .where(
                    ProviderBudgetUsage.provider_code == setting.provider.value,
                    ProviderBudgetUsage.capability == setting.capability.value,
                    ProviderBudgetUsage.budget_date == budget_date,
                )
                .with_for_update()
            )
            if usage is None:
                usage = ProviderBudgetUsage(
                    provider_code=setting.provider.value,
                    capability=setting.capability.value,
                    budget_date=budget_date,
                    used_count=0,
                )
                session.add(usage)
                await session.flush()

            usage_rows = list(
                await session.execute(
                    select(
                        ProviderBudgetUsage.capability,
                        ProviderBudgetUsage.used_count,
                    ).where(
                        ProviderBudgetUsage.provider_code == setting.provider.value,
                        ProviderBudgetUsage.budget_date == budget_date,
                    )
                )
            )
            total_used = sum(count for _, count in usage_rows)
            used_by_capability = dict(usage_rows)
            total_ceiling = self._total_ceiling(
                policy, setting.capability, used_by_capability
            )
            if total_used >= total_ceiling:
                denial = "PROVIDER_DAILY_BUDGET_EXHAUSTED"
            elif usage.used_count >= capability_limit:
                denial = "PROVIDER_CAPABILITY_BUDGET_EXHAUSTED"
            elif (
                usage.last_request_at is not None
                and now < usage.last_request_at + timedelta(seconds=min_interval)
            ):
                denial = "PROVIDER_MIN_INTERVAL_LIMITED"
                retry_after_seconds = (
                    usage.last_request_at
                    + timedelta(seconds=min_interval)
                    - now
                ).total_seconds()
            else:
                active_total = int(
                    await session.scalar(
                        select(func.count(ProviderRequestLease.id)).where(
                            ProviderRequestLease.provider_code
                            == setting.provider.value,
                            ProviderRequestLease.released_at.is_(None),
                            ProviderRequestLease.expires_at > now,
                        )
                    )
                    or 0
                )
                active_capability = int(
                    await session.scalar(
                        select(func.count(ProviderRequestLease.id)).where(
                            ProviderRequestLease.provider_code
                            == setting.provider.value,
                            ProviderRequestLease.capability == setting.capability.value,
                            ProviderRequestLease.released_at.is_(None),
                            ProviderRequestLease.expires_at > now,
                        )
                    )
                    or 0
                )
                if active_total >= policy.max_concurrency:
                    denial = "PROVIDER_TOTAL_CONCURRENCY_LIMITED"
                    retry_after_seconds = 0.05
                elif active_capability >= setting.concurrency:
                    denial = "PROVIDER_CAPABILITY_CONCURRENCY_LIMITED"
                    retry_after_seconds = 0.05

            if denial is not None:
                usage.latest_limit_reason = denial
                usage.latest_limited_at = now
            else:
                usage.used_count += 1
                usage.last_request_at = now
                session.add(
                    ProviderRequestLease(
                        token=token,
                        provider_code=setting.provider.value,
                        capability=setting.capability.value,
                        acquired_at=now,
                        expires_at=now
                        + timedelta(seconds=max(5.0, setting.timeout_seconds + 5.0)),
                    )
                )
        if denial is not None:
            raise ProviderHttpError(
                denial, retry_after_seconds=retry_after_seconds
            )
        return BudgetLease(token)

    async def release(self, lease: BudgetLease) -> None:
        async with self._database.transaction() as session:
            await session.execute(
                update(ProviderRequestLease)
                .where(
                    ProviderRequestLease.token == lease.token,
                    ProviderRequestLease.released_at.is_(None),
                )
                .values(released_at=func.now())
            )

    async def snapshot(self, provider: ProviderCode) -> dict[str, Any]:
        async with self._database.transaction() as session:
            now = await session.scalar(select(func.clock_timestamp()))
            if not isinstance(now, datetime):
                now = datetime.now(UTC)
            policy = await self._latest_policy(session, provider)
            timezone = self._timezone(policy.reset_timezone)
            budget_date = now.astimezone(timezone).date()
            rows = list(
                await session.scalars(
                    select(ProviderBudgetUsage)
                    .where(
                        ProviderBudgetUsage.provider_code == provider.value,
                        ProviderBudgetUsage.budget_date == budget_date,
                    )
                    .order_by(ProviderBudgetUsage.capability)
                )
            )
            limits = await self._capability_limits(session, provider)
            total_used = sum(row.used_count for row in rows)
            latest = max(
                (row for row in rows if row.latest_limited_at is not None),
                key=lambda row: row.latest_limited_at,
                default=None,
            )
            reset_at = datetime.combine(
                budget_date + timedelta(days=1), time.min, tzinfo=timezone
            ).astimezone(UTC)
            by_capability = {row.capability: row for row in rows}
            return {
                "provider_code": provider.value,
                "budget_date": budget_date.isoformat(),
                "daily_limit": policy.daily_limit,
                "used": total_used,
                "remaining": max(0, policy.daily_limit - total_used),
                "reset_timezone": policy.reset_timezone,
                "reset_at": reset_at,
                "latest_limit_reason": latest.latest_limit_reason if latest else None,
                "latest_limited_at": latest.latest_limited_at if latest else None,
                "realtime_reserved": policy.realtime_reserved,
                "daily_reserved": policy.daily_reserved,
                "capabilities": [
                    {
                        "capability": capability,
                        "daily_limit": limit,
                        "used": by_capability.get(capability).used_count
                        if capability in by_capability
                        else 0,
                        "remaining": max(
                            0,
                            limit
                            - (
                                by_capability.get(capability).used_count
                                if capability in by_capability
                                else 0
                            ),
                        ),
                        "latest_limit_reason": (
                            by_capability.get(capability).latest_limit_reason
                            if capability in by_capability
                            else None
                        ),
                    }
                    for capability, limit in sorted(limits.items())
                ],
            }

    async def _configuration(
        self, session: Any, setting: ProviderRouteSetting
    ) -> tuple[ProviderBudgetPolicy, int, float]:
        policy = await self._latest_policy(session, setting.provider)
        row = await session.scalar(
            select(ProviderCapabilitySetting)
            .where(
                ProviderCapabilitySetting.provider_code == setting.provider.value,
                ProviderCapabilitySetting.capability == setting.capability.value,
            )
            .order_by(ProviderCapabilitySetting.config_version.desc())
            .limit(1)
        )
        if row is None:
            return policy, 50_000, max(0.0, 1.0 / setting.rate_per_second)
        return policy, row.daily_limit, row.min_interval_seconds

    async def _latest_policy(
        self, session: Any, provider: ProviderCode
    ) -> ProviderBudgetPolicy:
        policy = await session.scalar(
            select(ProviderBudgetPolicy)
            .where(ProviderBudgetPolicy.provider_code == provider.value)
            .order_by(ProviderBudgetPolicy.config_version.desc())
            .limit(1)
        )
        return policy or ProviderBudgetPolicy(
            config_version=0,
            provider_code=provider.value,
            daily_limit=50_000,
            reset_timezone="Asia/Shanghai",
            max_concurrency=8,
            realtime_reserved=500,
            daily_reserved=500,
        )

    async def _capability_limits(
        self, session: Any, provider: ProviderCode
    ) -> dict[str, int]:
        latest = (
            select(
                ProviderCapabilitySetting.capability.label("capability"),
                func.max(ProviderCapabilitySetting.config_version).label("version"),
            )
            .where(ProviderCapabilitySetting.provider_code == provider.value)
            .group_by(ProviderCapabilitySetting.capability)
            .subquery()
        )
        rows = await session.execute(
            select(
                ProviderCapabilitySetting.capability,
                ProviderCapabilitySetting.daily_limit,
            ).join(
                latest,
                (ProviderCapabilitySetting.capability == latest.c.capability)
                & (ProviderCapabilitySetting.config_version == latest.c.version),
            )
        )
        return {capability: limit for capability, limit in rows}

    @staticmethod
    def _total_ceiling(
        policy: ProviderBudgetPolicy,
        capability: ProviderCapability,
        used_by_capability: dict[str, int],
    ) -> int:
        realtime_used = used_by_capability.get(
            ProviderCapability.REALTIME_QUOTE_BATCH.value, 0
        )
        daily_used = used_by_capability.get(
            ProviderCapability.DAILY_BAR_UNADJUSTED.value, 0
        )
        protected_realtime = max(0, policy.realtime_reserved - realtime_used)
        protected_daily = max(0, policy.daily_reserved - daily_used)
        if capability is ProviderCapability.REALTIME_QUOTE_BATCH:
            return policy.daily_limit - protected_daily
        if capability is ProviderCapability.DAILY_BAR_UNADJUSTED:
            return policy.daily_limit - protected_realtime
        return policy.daily_limit - protected_realtime - protected_daily

    @staticmethod
    def _timezone(value: str) -> ZoneInfo:
        try:
            return ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ProviderHttpError("PROVIDER_RESET_TIMEZONE_INVALID") from error
