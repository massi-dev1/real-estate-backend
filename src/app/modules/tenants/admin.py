"""Platform-admin operations: cross-tenant metrics + impersonation (§8.16).

Both are platform-back-office concerns (tenant-exempt routes, platform RBAC).
Impersonation is audit-logged (§10.11) and time-boxed via a short-lived
special-purpose access token carrying an ``imp`` claim — never a normal login
session and never a refresh token.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import ConflictError, NotFoundError
from app.core.security import create_access_token
from app.modules.tenants.audit import AuditActor, AuditService, build_audit_service
from app.modules.tenants.models import TenantStatus, TenantUsage
from app.modules.tenants.repository import TenantRepository
from app.modules.users.service import UserService, get_user_service


@dataclass(frozen=True, slots=True)
class TenantMetric:
    tenant_id: uuid.UUID
    tenant_name: str
    status: str
    plan: str
    listings_count: int
    agents_count: int
    storage_bytes: int


@dataclass(frozen=True, slots=True)
class PlatformMetrics:
    total_tenants: int
    active_tenants: int
    trial_tenants: int
    suspended_tenants: int
    total_listings: int
    total_agents: int
    tenants: list[TenantMetric]


@dataclass(frozen=True, slots=True)
class ImpersonationGrant:
    access_token: str
    expires_in: int
    tenant_id: uuid.UUID
    tenant_slug: str
    acting_as_user_id: uuid.UUID


class PlatformAdminService:
    def __init__(
        self,
        repo: TenantRepository,
        users: UserService,
        audit: AuditService,
        settings: Settings,
    ) -> None:
        self.repo = repo
        self.users = users
        self.audit = audit
        self.settings = settings

    async def metrics(self) -> PlatformMetrics:
        """Cross-tenant snapshot (§8.16). Reads the O(1) ``tenant_usage`` counters
        (kept current at write-time) plus the per-status tenant counts — a light
        live version; a later migration can back this with Part 21's rollups for
        richer time-series."""
        tenants = await self.repo.list_all()
        usage_by_tenant = await self._usage_by_tenant()
        status_counts = await self.repo.count_by_status()
        rows: list[TenantMetric] = []
        total_listings = 0
        total_agents = 0
        for tenant in tenants:
            usage = usage_by_tenant.get(tenant.id)
            listings = usage.listings_count if usage else 0
            agents = usage.agents_count if usage else 0
            storage = usage.storage_bytes if usage else 0
            total_listings += listings
            total_agents += agents
            rows.append(
                TenantMetric(
                    tenant_id=tenant.id,
                    tenant_name=tenant.name,
                    status=tenant.status.value,
                    plan=tenant.plan,
                    listings_count=listings,
                    agents_count=agents,
                    storage_bytes=storage,
                )
            )
        return PlatformMetrics(
            total_tenants=len(tenants),
            active_tenants=status_counts.get(TenantStatus.ACTIVE.value, 0),
            trial_tenants=status_counts.get(TenantStatus.TRIAL.value, 0),
            suspended_tenants=status_counts.get(TenantStatus.SUSPENDED.value, 0),
            total_listings=total_listings,
            total_agents=total_agents,
            tenants=rows,
        )

    async def _usage_by_tenant(self) -> dict[uuid.UUID, TenantUsage]:
        rows = (await self.repo.session.execute(select(TenantUsage))).scalars().all()
        return {row.tenant_id: row for row in rows}

    async def impersonate(
        self, tenant_id: uuid.UUID, *, actor: AuditActor
    ) -> ImpersonationGrant:
        """Mint a time-boxed impersonation token for the tenant's admin (§8.16).

        Audit-logged (§10.11). The token carries an ``imp`` claim (the platform
        staff id) so the frontend can show an "impersonation active" banner, and
        it has no refresh token — it dies at its short TTL and cannot be renewed.
        """
        tenant = await self.repo.get(tenant_id)
        if tenant is None:
            raise NotFoundError("Tenant not found.")
        if tenant.status is TenantStatus.SUSPENDED:
            raise ConflictError("Cannot impersonate into a suspended tenant.")
        target = await self.users.first_admin_for_tenant(tenant_id)
        if target is None:
            raise ConflictError("This tenant has no admin account to impersonate.")

        ttl = self.settings.impersonation_token_ttl_seconds
        access_token, _ = create_access_token(
            user_id=target.id,
            tenant_id=tenant_id,
            role=target.role.value,
            settings=self.settings,
            ttl_seconds=ttl,
            impersonator_id=actor.user_id,
        )
        self.audit.record(
            action="tenant.impersonate",
            actor=actor,
            tenant_id=tenant_id,
            target=str(target.id),
            metadata={"tenant_slug": tenant.slug, "acting_as_role": target.role.value},
        )
        return ImpersonationGrant(
            access_token=access_token,
            expires_in=ttl,
            tenant_id=tenant_id,
            tenant_slug=tenant.slug,
            acting_as_user_id=target.id,
        )


def build_platform_admin_service(session: AsyncSession) -> PlatformAdminService:
    return PlatformAdminService(
        TenantRepository(session),
        get_user_service(session),
        build_audit_service(session),
        get_settings(),
    )
