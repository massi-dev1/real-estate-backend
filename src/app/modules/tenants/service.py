"""Tenant business logic: platform CRUD, domain management, host resolution.

Domain → tenant lookups are cached in Redis (TTL from settings, §4.1); every
mutation that changes what a domain serves (status, settings, name, domain
set) invalidates the affected cache keys.
"""

import json
import secrets
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import structlog
from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.cache import bump_version
from app.core.config import get_settings
from app.core.database import SessionDep
from app.core.exceptions import ConflictError, NotFoundError
from app.core.pagination import InvalidCursorError, clamp_limit, decode_cursor, encode_cursor
from app.core.tenancy import TenantContext
from app.modules.tenants.dns_verify import (
    TxtResolver,
    default_txt_lookup,
    txt_record_present,
)
from app.modules.tenants.models import (
    DomainVerificationStatus,
    Tenant,
    TenantDomain,
    TenantStatus,
)
from app.modules.tenants.plans import DEFAULT_PLAN, PLANS
from app.modules.tenants.repository import TenantRepository
from app.modules.tenants.schemas import TenantCreate, TenantDomainCreate, TenantUpdate
from app.modules.tenants.usage import UsageService, UsageSnapshot, build_usage_boundary

logger = structlog.get_logger(__name__)

_KNOWN_PLANS = frozenset(PLANS)

# ``settings`` namespaces that configure a backend integration and have no
# public consumer. ``syndication`` holds each portal's partner ``api_key``.
PRIVATE_SETTINGS_NAMESPACES = frozenset({"syndication"})

# Substrings that mark a value as a credential wherever it appears in the blob.
# Belt-and-braces behind the namespace drop above: a future namespace that
# stores a secret is redacted even if nobody remembers to list it here.
_SECRET_KEY_MARKERS = ("secret", "token", "password", "api_key", "apikey", "private_key")

_REDACTED = "[redacted]"


def _scrub_secrets(value: Any) -> Any:
    """Recursively redact credential-shaped keys in a settings sub-tree."""
    if isinstance(value, dict):
        return {
            key: (
                _REDACTED
                if any(marker in key.lower() for marker in _SECRET_KEY_MARKERS)
                else _scrub_secrets(inner)
            )
            for key, inner in value.items()
        }
    if isinstance(value, list):
        return [_scrub_secrets(item) for item in value]
    return value


def _new_verification_token() -> str:
    """DNS TXT-challenge value the tenant publishes to prove domain control."""
    return f"realestate-verify={secrets.token_urlsafe(24)}"


def _domain_cache_key(domain: str) -> str:
    return f"tenant:domain:{domain}"


def _to_context(tenant: Tenant) -> TenantContext:
    return TenantContext(
        id=tenant.id,
        slug=tenant.slug,
        name=tenant.name,
        status=tenant.status.value,
        settings=tenant.settings,
        plan=tenant.plan,
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
    def __init__(
        self, repo: TenantRepository, redis: Redis, usage: UsageService | None = None
    ) -> None:
        self.repo = repo
        self.redis = redis
        self._usage = usage

    @property
    def usage(self) -> UsageService:
        assert self._usage is not None, "this TenantService was built without usage"
        return self._usage

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

    def _invalidate_site_config_after_commit(self, tenant_id: uuid.UUID) -> None:
        """Bump the ``site_config`` cache version (§11) so the cached
        ``GET /site/config`` payload is retired after a settings/plan/status
        change. Post-commit for the same reason as the domain cache above."""

        async def _bump() -> None:
            await bump_version(self.redis, str(tenant_id), "site_config")

        self.repo.after_commit(_bump)

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

        trial_days = get_settings().trial_length_days
        tenant = Tenant(
            name=data.name,
            slug=data.slug,
            settings=data.settings,
            plan=DEFAULT_PLAN,
            trial_ends_at=datetime.now(UTC) + timedelta(days=trial_days),
        )
        tenant.domains.append(
            TenantDomain(
                domain=data.domain,
                is_primary=True,
                verification_token=_new_verification_token(),
            )
        )
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
        self._invalidate_site_config_after_commit(tenant_id)
        return await self._get_or_404(tenant_id)

    @staticmethod
    def public_settings(raw: dict[str, Any]) -> dict[str, Any]:
        """The subset of ``tenants.settings`` safe to serve anonymously.

        ``settings`` is a free-form JSONB blob the platform PATCHes, and agencies
        keep arbitrary branding keys in it — so this cannot be a strict allowlist
        without breaking their sites. It is instead a two-layer scrub:

        1. Whole namespaces that exist only to configure a backend integration
           are dropped (``syndication`` stores each portal's partner
           ``api_key``). These have no public consumer at all.
        2. Any key that names a credential is redacted at every depth, so a
           namespace added later cannot silently reintroduce the leak.

        Without this, ``GET /site/config`` — unauthenticated, resolved purely
        from the Host header — returned the raw blob, publishing the very portal
        API key that ``GET /portal/syndication/settings`` deliberately refuses
        to echo back to the tenant's own admin.
        """
        return {
            key: _scrub_secrets(value)
            for key, value in raw.items()
            if key not in PRIVATE_SETTINGS_NAMESPACES
        }

    async def get_settings_key(self, tenant_id: uuid.UUID, key: str) -> dict[str, object]:
        """One top-level ``settings`` sub-object (e.g. ``syndication``), or an
        empty dict — boundary accessor for tenant-side modules that own a
        settings namespace (syndication §8.14) without reaching into the tenants
        table or the platform-only whole-blob PATCH."""
        tenant = await self._get_or_404(tenant_id)
        value = tenant.settings.get(key)
        return dict(value) if isinstance(value, dict) else {}

    async def replace_settings_key(
        self, tenant_id: uuid.UUID, key: str, value: dict[str, object]
    ) -> dict[str, object]:
        """Replace one top-level ``settings`` sub-object, leaving every other
        namespace untouched — the tenant-scoped counterpart to the platform's
        whole-blob PATCH. Assigns a fresh dict so SQLAlchemy flags the JSONB
        column dirty (an in-place mutation would not), and invalidates the
        domain cache exactly like ``update`` does."""
        tenant = await self._get_or_404(tenant_id)
        tenant.settings = {**tenant.settings, key: value}
        await self.repo.flush()
        self._invalidate_domains_after_commit([d.domain for d in tenant.domains])
        self._invalidate_site_config_after_commit(tenant_id)
        return value

    async def set_status(self, tenant_id: uuid.UUID, status: TenantStatus) -> Tenant:
        tenant = await self._get_or_404(tenant_id)
        tenant.status = status
        await self.repo.flush()
        self._invalidate_domains_after_commit([d.domain for d in tenant.domains])
        return await self._get_or_404(tenant_id)

    # ---- plan / lifecycle (§8.16) ----

    async def set_plan(self, tenant_id: uuid.UUID, plan: str) -> Tenant:
        """Change the quota tier. An unknown plan key is a 422 (the caller
        validates against the code-owned plans table first)."""
        if plan not in _KNOWN_PLANS:
            raise ConflictError(f"Unknown plan '{plan}'.")
        tenant = await self._get_or_404(tenant_id)
        tenant.plan = plan
        await self.repo.flush()
        # The plan rides on the cached TenantContext — invalidate so a
        # quota check on the next request reads the new tier.
        self._invalidate_domains_after_commit([d.domain for d in tenant.domains])
        self._invalidate_site_config_after_commit(tenant_id)
        return await self._get_or_404(tenant_id)

    async def start_offboard(self, tenant_id: uuid.UUID) -> Tenant:
        """Begin offboarding: suspend the tenant now (site goes 402), stamp the
        offboard time, and schedule deletion after the undo window. The export
        job runs from the offboard task; the purge from the deletion sweep."""
        tenant = await self._get_or_404(tenant_id)
        now = datetime.now(UTC)
        tenant.status = TenantStatus.SUSPENDED
        tenant.offboarding_at = now
        tenant.deletion_scheduled_at = now + timedelta(
            days=get_settings().offboard_deletion_delay_days
        )
        await self.repo.flush()
        self._invalidate_domains_after_commit([d.domain for d in tenant.domains])
        # Export the tenant's data after commit (offboard step 1). Lazy import —
        # the task module imports this service.
        tenant_id_str = str(tenant_id)

        async def _enqueue_export() -> None:
            from app.workers.tasks.tenants import export_tenant

            export_tenant.delay(tenant_id_str)

        self.repo.after_commit(_enqueue_export)
        return await self._get_or_404(tenant_id)

    async def cancel_offboard(self, tenant_id: uuid.UUID) -> Tenant:
        """Undo an offboard before the purge runs — reactivate and clear the
        deletion schedule (a purged tenant is gone; this only works pre-purge)."""
        tenant = await self._get_or_404(tenant_id)
        if tenant.deleted_at is not None:
            raise ConflictError("This tenant has already been deleted.")
        tenant.status = TenantStatus.ACTIVE
        tenant.offboarding_at = None
        tenant.deletion_scheduled_at = None
        await self.repo.flush()
        self._invalidate_domains_after_commit([d.domain for d in tenant.domains])
        return await self._get_or_404(tenant_id)

    # ---- domain verification (§8.16) ----

    async def verify_domain(
        self,
        tenant_id: uuid.UUID,
        domain_id: uuid.UUID,
        *,
        resolver: TxtResolver | None = None,
    ) -> TenantDomain:
        """Check the domain's DNS TXT challenge and flip its status. Idempotent:
        an already-verified domain re-verifies to the same result. ``resolver``
        defaults to the live DNS lookup, resolved at call time (so tests can
        monkeypatch the module global)."""
        await self._get_or_404(tenant_id)
        domain = await self.repo.get_domain(tenant_id, domain_id)
        if domain is None:
            raise NotFoundError("Domain not found.")
        if not domain.verification_token:
            raise ConflictError("This domain has no verification challenge.")
        present = await txt_record_present(
            domain.domain,
            domain.verification_token,
            resolver=resolver or default_txt_lookup,
        )
        if present:
            domain.verification_status = DomainVerificationStatus.VERIFIED
            domain.verified_at = datetime.now(UTC)
        else:
            domain.verification_status = DomainVerificationStatus.FAILED
        await self.repo.flush()
        return domain

    # ---- usage / site config (§8.16) ----

    async def usage_snapshot(self, tenant_id: uuid.UUID) -> UsageSnapshot:
        return await self.usage.snapshot(tenant_id)

    async def add_domain(self, tenant_id: uuid.UUID, data: TenantDomainCreate) -> Tenant:
        tenant = await self._get_or_404(tenant_id)
        if await self.repo.domain_exists(data.domain):
            raise ConflictError(f"Domain '{data.domain}' is already in use.")
        if data.is_primary:
            for existing in tenant.domains:
                existing.is_primary = False
        tenant.domains.append(
            TenantDomain(
                domain=data.domain,
                is_primary=data.is_primary,
                verification_token=_new_verification_token(),
            )
        )
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
    return TenantService(
        TenantRepository(session),
        redis=request.app.state.redis,
        usage=build_usage_boundary(session),
    )


def build_tenant_boundary(session: AsyncSession, redis: Redis) -> TenantService:
    """Construct a :class:`TenantService` for tenant-side dependents (syndication
    §8.14) that need its settings-namespace boundary without pulling ``request``
    into their own factory signature."""
    return TenantService(
        TenantRepository(session), redis=redis, usage=build_usage_boundary(session)
    )


TenantServiceDep = Annotated[TenantService, Depends(get_tenant_service)]
