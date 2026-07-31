import hashlib
import secrets
from datetime import datetime
from math import ceil

from sqlalchemy import select, text, update

from long_invest.modules.auth.models import LoginRateLimitAttempt
from long_invest.modules.auth.rate_limit import RateLimitConfig, RateLimitDecision

_COUNTED_OUTCOMES = ("PENDING", "FAILED")


class PostgresLoginRateLimiter:
    def __init__(self, database, config: RateLimitConfig | None = None) -> None:
        self._database = database
        self._config = config or RateLimitConfig()

    async def check(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
    ) -> RateLimitDecision:
        ip_digest, username_digest = _digests(ip, username)
        cutoff = now - self._config.window
        async with self._database.transaction() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": "auth-login-rate-limit"},
            )
            attempts = list(
                await session.scalars(
                    select(LoginRateLimitAttempt)
                    .where(
                        LoginRateLimitAttempt.outcome.in_(_COUNTED_OUTCOMES),
                        LoginRateLimitAttempt.occurred_at > cutoff,
                    )
                    .order_by(LoginRateLimitAttempt.occurred_at)
                )
            )
            dimensions = (
                (
                    [item for item in attempts if item.ip_digest == ip_digest],
                    self._config.per_ip,
                ),
                (
                    [
                        item
                        for item in attempts
                        if item.username_digest == username_digest
                    ],
                    self._config.per_username,
                ),
                (attempts, self._config.global_failures),
            )
            blocked = [
                (items, limit) for items, limit in dimensions if len(items) >= limit
            ]
            if blocked:
                retry_after = max(
                    ceil(
                        (
                            items[len(items) - limit].occurred_at
                            + self._config.window
                            - now
                        ).total_seconds()
                    )
                    for items, limit in blocked
                )
                return RateLimitDecision(
                    allowed=False,
                    retry_after_seconds=max(1, retry_after),
                )

            reservation_id = secrets.token_hex(16)
            session.add(
                LoginRateLimitAttempt(
                    reservation_id=reservation_id,
                    ip_digest=ip_digest,
                    username_digest=username_digest,
                    outcome="PENDING",
                    occurred_at=now,
                )
            )
            await session.flush()
            return RateLimitDecision(True, reservation_id=reservation_id)

    async def record_failure(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
        reservation_id: str | None = None,
    ) -> None:
        ip_digest, username_digest = _digests(ip, username)
        async with self._database.transaction() as session:
            if reservation_id is not None:
                result = await session.execute(
                    update(LoginRateLimitAttempt)
                    .where(
                        LoginRateLimitAttempt.reservation_id == reservation_id,
                        LoginRateLimitAttempt.ip_digest == ip_digest,
                        LoginRateLimitAttempt.username_digest == username_digest,
                        LoginRateLimitAttempt.outcome == "PENDING",
                    )
                    .values(outcome="FAILED")
                )
                if result.rowcount:
                    return
                existing = await session.scalar(
                    select(LoginRateLimitAttempt.id).where(
                        LoginRateLimitAttempt.reservation_id == reservation_id
                    )
                )
                if existing is not None:
                    return
            session.add(
                LoginRateLimitAttempt(
                    reservation_id=reservation_id or secrets.token_hex(16),
                    ip_digest=ip_digest,
                    username_digest=username_digest,
                    outcome="FAILED",
                    occurred_at=now,
                )
            )

    async def record_success(
        self,
        *,
        ip: str,
        username: str,
        now: datetime,
        reservation_id: str | None = None,
    ) -> None:
        del now
        if reservation_id is None:
            return
        ip_digest, username_digest = _digests(ip, username)
        async with self._database.transaction() as session:
            await session.execute(
                update(LoginRateLimitAttempt)
                .where(
                    LoginRateLimitAttempt.reservation_id == reservation_id,
                    LoginRateLimitAttempt.ip_digest == ip_digest,
                    LoginRateLimitAttempt.username_digest == username_digest,
                    LoginRateLimitAttempt.outcome == "PENDING",
                )
                .values(outcome="SUCCEEDED")
            )


def _digests(ip: str, username: str) -> tuple[str, str]:
    return (
        hashlib.sha256(ip.encode()).hexdigest(),
        hashlib.sha256(username.casefold().encode()).hexdigest(),
    )
