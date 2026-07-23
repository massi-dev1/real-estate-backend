"""Internal edge-support endpoints (§16) — reachable only from the reverse
proxy's private network, never the public internet (the proxy 404s
``/internal/*`` from outside, and the tenant middleware exempts the prefix).

``GET /internal/tls-check?domain=`` backs Caddy's on-demand-TLS *ask* handler:
Caddy obtains a certificate for a host only if this returns 200, so an attacker
cannot point arbitrary hostnames at us and exhaust the ACME rate limit. A domain
maps to a tenant (even a suspended one — it still needs a cert to serve its
maintenance page) → 200; anything else → 404.
"""

import structlog
from fastapi import APIRouter, Query, Request, Response, status

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
