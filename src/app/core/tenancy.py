"""Tenant resolution: per-request tenant context + middleware (§4.1).

The middleware resolves ``Host`` → tenant before anything else touches the
request. Resolution itself is delegated to a resolver object placed on
``app.state.tenant_resolver`` by the app factory (the tenants module provides
it) — core never imports module code.
"""

import uuid
from dataclasses import dataclass
from typing import Annotated, Any, Protocol

import structlog
from fastapi import Depends, Request, status
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.exceptions import NotFoundError, problem_response

logger = structlog.get_logger(__name__)

# Paths served without a tenant: infra endpoints, the platform back-office, and
# the billing webhook (verified by signature, §10.9 — no tenant context).
# ``/internal`` carries edge-support endpoints (e.g. Caddy's on-demand-TLS
# ask handler, §16) that are called *about* a domain, not *for* one, and are
# never exposed past the reverse proxy's private network.
TENANT_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/healthz",
    "/readyz",
    "/internal",
    "/docs",
    "/openapi.json",
    "/api/v1/platform",
    "/api/v1/billing",
)


@dataclass(frozen=True, slots=True)
class TenantContext:
    """The resolved tenant, attached to ``request.state.tenant``."""

    id: uuid.UUID
    slug: str
    name: str
    status: str
    settings: dict[str, Any]
    # Quota tier (§8.16) — carried on the context so a write-time quota check
    # reads the plan limits without a separate DB hit. Defaulted so an older
    # cached payload (pre-Part-22) deserializes without KeyError.
    plan: str = "trial"


class TenantResolver(Protocol):
    async def resolve(self, domain: str) -> TenantContext | None: ...


def _host_from_scope(scope: Scope) -> str:
    headers: dict[bytes, bytes] = dict(scope.get("headers") or [])
    host = headers.get(b"host", b"").decode("latin-1")
    return host.split(":")[0].strip().lower()


class TenantResolutionMiddleware:
    """Resolves the tenant from the Host header for every non-exempt request.

    Unknown domain → 404, suspended tenant → 402 — both as problem+json,
    before any route code runs.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(TENANT_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        resolver: TenantResolver = scope["app"].state.tenant_resolver
        host = _host_from_scope(scope)
        tenant = await resolver.resolve(host) if host else None

        if tenant is None:
            logger.info("tenant_unresolved", host=host)
            response = problem_response(
                Request(scope),
                status_code=status.HTTP_404_NOT_FOUND,
                slug="unknown-tenant",
                title="Unknown Tenant Domain",
                detail="No agency site is configured for this domain.",
            )
            await response(scope, receive, send)
            return

        if tenant.status == "suspended":
            response = problem_response(
                Request(scope),
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                slug="tenant-suspended",
                title="Tenant Suspended",
                detail="This agency site is temporarily unavailable.",
            )
            await response(scope, receive, send)
            return

        scope.setdefault("state", {})["tenant"] = tenant
        structlog.contextvars.bind_contextvars(tenant_id=str(tenant.id))
        await self.app(scope, receive, send)


def get_current_tenant(request: Request) -> TenantContext:
    tenant = getattr(request.state, "tenant", None)
    if not isinstance(tenant, TenantContext):
        # Only reachable from a route mounted on an exempt path by mistake.
        raise NotFoundError("No tenant is associated with this request.")
    return tenant


TenantDep = Annotated[TenantContext, Depends(get_current_tenant)]
