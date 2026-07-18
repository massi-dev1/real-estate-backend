"""DB access for valuation requests. Every method takes ``tenant_id``
(golden rule §5); the anonymous step flow addresses rows by id only after the
service has verified the HMAC token pinning that id to the tenant.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.valuations.models import ValuationRequest


class ValuationsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, tenant_id: uuid.UUID, request_id: uuid.UUID) -> ValuationRequest | None:
        stmt = select(ValuationRequest).where(
            ValuationRequest.tenant_id == tenant_id, ValuationRequest.id == request_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def add(self, obj: ValuationRequest) -> None:
        self.session.add(obj)

    async def flush(self) -> None:
        await self.session.flush()
