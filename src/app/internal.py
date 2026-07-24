"""Internal edge-support endpoints (§16) — reachable only from the reverse
proxy's private network, never the public internet (the proxy 404s
``/internal/*`` from outside, and the tenant middleware exempts the prefix).

``GET /internal/tls-check?domain=`` backs Caddy's on-demand-TLS *ask* handler:
Caddy obtains a certificate for a host only if this returns 200, so an attacker
cannot point arbitrary hostnames at us and exhaust the ACME rate limit. A domain
maps to a tenant (even a suspended one — it still needs a cert to serve its
maintenance page) → 200; anything else → 404.

``GET /internal/metrics`` is the Prometheus scrape target (§14). It sits behind
the same private prefix rather than at a public ``/metrics`` because the
exposition text leaks route templates, traffic volumes and business counters;
when ``metrics_auth_token`` is configured it additionally requires a bearer
token, so the endpoint is safe even if the proxy rule is ever misconfigured.
"""

import hmac

import structlog
from fastapi import APIRouter, Query, Request, Response, status

from app.core.metrics import collect_runtime_metrics, render_metrics

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/tls-check")
async def tls_check(
    request: Request, domain: str = Query(min_length=1, max_length=253)
) -> Response:
    resolver = request.app.state.tenant_resolver
    host = domain.split(":")[0].strip().lower()
    tenant = await resolver.resolve(host) if host else None
    if tenant is None:
        logger.info("tls_check_rejected", domain=host)
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return Response(status_code=status.HTTP_200_OK)


def _metrics_token_ok(request: Request, expected: str) -> bool:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(token, expected)


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    """Prometheus exposition (§14). 404 — not 403 — when metrics are disabled
    or the token is wrong: an unauthenticated caller learns nothing about
    whether this deployment exports metrics at all."""
    settings = request.app.state.settings
    if not settings.metrics_enabled:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    if settings.metrics_auth_token and not _metrics_token_ok(request, settings.metrics_auth_token):
        logger.info("metrics_scrape_rejected")
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    await collect_runtime_metrics(
        getattr(request.app.state, "engine", None), getattr(request.app.state, "redis", None)
    )
    payload, content_type = render_metrics()
    return Response(content=payload, media_type=content_type)
