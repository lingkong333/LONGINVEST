from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import httpx

from long_invest.modules.market_data.repository import CorporateActionRepository
from long_invest.modules.market_data.service import (
    CorporateActionFactInput,
    CorporateActionService,
    RecordCorporateActionFetch,
)
from long_invest.modules.providers.contracts import CorporateActionRequest
from long_invest.modules.providers.retry import ProviderHttpError
from long_invest.platform.database.engine import Database


class CorporateActionCollectionApplication:
    def __init__(
        self,
        database: Database,
        *,
        providers: Any,
        repository_factory: Callable[..., Any] = CorporateActionRepository,
        service_factory: Callable[..., Any] = CorporateActionService,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        contract_version: str = "corporate-actions-v1",
    ) -> None:
        self._database = database
        self._providers = providers
        self._repository_factory = repository_factory
        self._service_factory = service_factory
        self._clock = clock
        self._contract_version = contract_version

    async def collect(
        self,
        *,
        batch_id: UUID,
        security_id: UUID,
        symbol: str,
        start_date: date,
        end_date: date,
        deadline: datetime,
    ) -> UUID:
        request = CorporateActionRequest(symbol, start_date, end_date)
        error_code: str | None = None
        records = ()
        try:
            result = await self._providers.corporate_actions(request, deadline)
            if result.batch_error_code is not None or result.failures:
                error_code = result.batch_error_code or result.failures[0].code
            else:
                records = result.items
        except (
            ProviderHttpError,
            httpx.HTTPError,
            RuntimeError,
            TimeoutError,
        ) as error:
            error_code = str(getattr(error, "code", "PROVIDER_FAILED"))
        fetched_at = self._clock()
        observed_at = max(
            (record.observed_at for record in records), default=fetched_at
        )
        command = RecordCorporateActionFetch(
            batch_id=batch_id,
            security_id=security_id,
            source="EASTMONEY",
            provider_contract_version=self._contract_version,
            coverage_start=start_date,
            coverage_end=end_date,
            observed_at=observed_at,
            fetched_at=fetched_at,
            succeeded=error_code is None,
            error_code=error_code,
            facts=tuple(
                CorporateActionFactInput(
                    source_event_id=record.source_event_id,
                    event_type=record.event_type.value,
                    event_date=record.event_date,
                    effective_date=record.effective_date,
                    published_at=record.published_at,
                    observed_at=record.observed_at,
                    adjustment_factor=record.adjustment_factor,
                    source_reference=record.source_reference,
                    raw_content_hash=record.raw_payload_hash,
                )
                for record in records
            ),
        )
        async with self._database.transaction() as session:
            service = self._service_factory(self._repository_factory(session))
            return await service.record_fetch(command)
