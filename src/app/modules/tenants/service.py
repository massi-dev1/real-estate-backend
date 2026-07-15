"""Tenant business logic: platform CRUD, domain management, host resolution.

Domain → tenant lookups are cached in Redis (TTL from settings, §4.1); every
mutation that changes what a domain serves (status, settings, name, domain
set) invalidates the affected cache keys.
"""

import json
import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Annotated

import structlog
from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import SessionDep
from app.core.exceptions import ConflictError, NotFoundError
from app.core.pagination import InvalidCursorError, clamp_limit, decode_cursor, encode_cursor
from app.core.tenancy import TenantContext
from app.modules.tenants.models import Tenant, TenantDomain, TenantStatus
from app.modules.tenants.repository import TenantRepository
from app.modules.tenants.schemas import TenantCreate, TenantDomainCreate, TenantUpdate

logger = structlog.get_logger(__name__)


def _domain_cache_key(domain: str) -> str:
    return f"tenant:domain:{domain}"


def _to_context(tenant: Tenant) -> TenantContext:
    return TenantContext(
        id=tenant.id,
        slug=tenant.slug,
        name=tenant.name,
        status=tenant.status.value,
        settings=tenant.settings,
    )


class DomainTenantResolver:
    """Host → TenantContext with a Redis cache in front of the DB lookup.

    App-lifetime object (built in the lifespan); used by the tenant-resolution
    middleware on every request, so a Redis failure degrades to DB lookups
    instead of failing the request.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        cache_ttl_seconds: int,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis
        self._ttl = cache_ttl_seconds

    async def resolve(self, domain: str) -> TenantContext | None:
        key = _domain_cache_key(domain)
        try:
            cached = await self._redis.get(key)
        except Exception:
            logger.warning("tenant_cache_read_failed", domain=domain)
            cached = None
        if cached:
            data = json.loads(cached)
            data["id"] = uuid.UUID(data["id"])
            return TenantContext(**data)

        async with self._session_factory() as session:
            tenant = await TenantRepository(session).get_by_domain(domain)
        if tenant is None:
            return None

        context = _to_context(tenant)
        payload = asdict(context)
        payload["id"] = str(context.id)
        try:
            await self._redis.set(key, json.dumps(payload), ex=self._ttl)
        except Exception:
            logger.warning("tenant_cache_write_failed", domain=domain)
        return context


class TenantService:
    def __init__(self, repo: TenantRepository, redis: Redis) -> None:
        self.repo = repo
        self.redis = redis

    def _invalidate_domains_after_commit(self, domains: list[str]) -> None:
        """Queue cache invalidation for after commit — invalidating earlier
        would let a concurrent request re-cache the pre-commit state for the
        full TTL (e.g. a suspended tenant staying reachable)."""
        keys = [_domain_cache_key(d) for d in domains]
        if not keys:
            return

        async def _invalidate() -> None:
            try:
                await self.redis.delete(*keys)
            except Exception:
                logger.warning("tenant_cache_invalidate_failed", domains=domains)

        self.repo.after_commit(_invalidate)

    async def _flush_or_conflict(self) -> None:
        """Uniqueness pre-checks race under concurrency; the DB constraint is
        the real guard — surface its violation as 409, not 500."""
        try:
            await self.repo.flush()
        except IntegrityError as exc:
            raise ConflictError("The slug or domain is already in use.") from exc

    async def _get_or_404(self, tenant_id: uuid.UUID) -> Tenant:
        tenant = await self.repo.get(tenant_id)
        if tenant is None:
            raise NotFoundError("Tenant not found.")
        return tenant

    async def create(self, data: TenantCreate) -> Tenant:
        if await self.repo.get_by_slug(data.slug) is not None:
            raise ConflictError(f"A tenant with slug '{data.slug}' already exists.")
        if await self.repo.domain_exists(data.domain):
            raise ConflictError(f"Domain '{data.domain}' is already in use.")

        tenant = Tenant(name=data.name, slug=data.slug, settings=data.settings)
        tenant.domains.append(TenantDomain(domain=data.domain, is_primary=True))
        self.repo.add(tenant)
        await self._flush_or_conflict()
        return await self._get_or_404(tenant.id)

    async def get(self, tenant_id: uuid.UUID) -> Tenant:
        return await self._get_or_404(tenant_id)

    async def list(
        self, *, cursor: str | None, limit: int | None
    ) -> tuple[list[Tenant], str | None, int]:
        page_size = clamp_limit(limit)
        after: tuple[datetime, uuid.UUID] | None = None
        if cursor is not None:
            values = decode_cursor(cursor)
            try:
                after = (
                    datetime.fromisoformat(values["created_at"]),
                    uuid.UUID(values["id"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise InvalidCursorError("The provided cursor is malformed.") from exc

        rows = await self.repo.list_page(after=after, limit=page_size)
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size:
            last = items[-1]
            next_cursor = encode_cursor(
                {"created_at": last.created_at.isoformat(), "id": str(last.id)}
            )
        total = await self.repo.count()
        return items, next_cursor, total

    async def update(self, tenant_id: uuid.UUID, data: TenantUpdate) -> Tenant:
        tenant = await self._get_or_404(tenant_id)
        patch = data.model_dump(exclude_unset=True)
        if "name" in patch:
            tenant.name = patch["name"]
        if "settings" in patch and patch["settings"] is not None:
            tenant.settings = patch["settings"]
        await self.repo.flush()
        self._invalidate_domains_after_commit([d.domain for d in tenant.domains])
        return await self._get_or_404(tenant_id)

    async def set_status(self, tenant_id: uuid.UUID, status: TenantStatus) -> Tenant:
        tenant = await self._get_or_404(tenant_id)
        tenant.status = status
        await self.repo.flush()
        self._invalidate_domains_after_commit([d.domain for d in tenant.domains])
        return await self._get_or_404(tenant_id)

    async def add_domain(self, tenant_id: uuid.UUID, data: TenantDomainCreate) -> Tenant:
        tenant = await self._get_or_404(tenant_id)
        if await self.repo.domain_exists(data.domain):
            raise ConflictError(f"Domain '{data.domain}' is already in use.")
        if data.is_primary:
            for existing in tenant.domains:
                existing.is_primary = False
        tenant.domains.append(TenantDomain(domain=data.domain, is_primary=data.is_primary))
        await self._flush_or_conflict()
        self._invalidate_domains_after_commit([d.domain for d in tenant.domains])
        return await self._get_or_404(tenant_id)

    async def remove_domain(self, tenant_id: uuid.UUID, domain_id: uuid.UUID) -> Tenant:
        await self._get_or_404(tenant_id)
        domain = await self.repo.get_domain(tenant_id, domain_id)
        if domain is None:
            raise NotFoundError("Domain not found.")
        if domain.is_primary:
            raise ConflictError("The primary domain cannot be removed.")
        removed = domain.domain
        await self.repo.delete_domain(domain)
        await self.repo.flush()
        self._invalidate_domains_after_commit([removed])
        return await self._get_or_404(tenant_id)


def get_tenant_service(session: SessionDep, request: Request) -> TenantService:
    return TenantService(TenantRepository(session), redis=request.app.state.redis)


TenantServiceDep = Annotated[TenantService, Depends(get_tenant_service)]
