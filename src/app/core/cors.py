"""Tenant-aware CORS (§10.1).

Starlette's ``CORSMiddleware`` takes a *static* origin list, which cannot work
for a multi-tenant platform whose allowed origins are the agency domains rows
in ``tenant_domains`` — a list that changes whenever a tenant is onboarded or
adds a domain, with no restart. Listing every agency domain in one env var and
handing it to every tenant would also be wrong: agency A's site would be
allowed to make credentialed cross-origin calls to agency B's API host.

So the allowlist is resolved per request:

* the request's own tenant comes from the ``Host`` header (the tenant
  middleware's job, but CORS runs *outside* it — a preflight ``OPTIONS`` never
  reaches route code — so this middleware resolves it itself, through the same
  Redis-cached ``DomainTenantResolver``);
* the ``Origin`` header's host is resolved the same way;
* the origin is reflected **only** when both resolve to the *same tenant id*.

That is one cached lookup each, and it means a domain is a valid origin
exactly for the tenant that owns it. Requiring ``verification_status ==
verified`` on top would be stricter, but it would also break the very first
thing a newly-onboarded agency does — the tenant middleware already serves
traffic on an unverified domain, and a domain only exists in the table because
the platform put it there, so tenant ownership (not DNS proof) is the property
that matters here.

``cors_origins`` from config stays as an **additive** allowlist for origins
that belong to no tenant: the platform back-office SPA and local dev
frontends. Those are matched exactly, and never widened to ``*`` — reflecting
``*`` alongside ``Access-Control-Allow-Credentials`` is forbidden by the spec
and, worse, is what makes a credentialed cross-origin read possible at all.
"""

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.middleware import API_CSP, SECURITY_HEADERS
from app.core.tenancy import TenantResolver

logger = structlog.get_logger(__name__)

ALLOW_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
# Explicit rather than reflecting whatever the browser asks for: the set of
# headers this API actually reads is small and known.
ALLOW_HEADERS = "Authorization, Content-Type, Accept-Language, X-Request-ID, Idempotency-Key"
# Response headers a browser client is allowed to read cross-origin.
EXPOSE_HEADERS = "X-Request-ID, Retry-After"
PREFLIGHT_MAX_AGE = "600"


def strip_port(host: str) -> str:
    """A bare hostname with any trailing port removed.

    Shared by the ``Origin`` and ``Host`` parses so the two can never disagree
    about what host a value names — a plain ``split(":")[0]`` mangles a
    bracketed IPv6 literal (``[::1]:8000`` becomes ``[``), which would break
    the same-host comparison for every IPv6 deployment.
    """
    host = host.strip()
    if host.startswith("["):
        # Bracketed IPv6 literal: keep the brackets, drop only a trailing port.
        literal, sep, _ = host.partition("]")
        if not sep:
            return ""
        return f"{literal}]".lower()
    return host.split(":")[0].strip().lower()


def origin_host(origin: str) -> str:
    """The bare hostname of an ``Origin`` value (scheme and port stripped).

    ``Origin`` is always ``scheme://host[:port]`` (or the literal ``null``),
    never a path, so this is a parse rather than a heuristic.
    """
    _, _, rest = origin.partition("://")
    if not rest:
        return ""
    return strip_port(rest.split("/")[0])


def _apply_baseline_headers(response: PlainTextResponse) -> None:
    """Stamp the baseline security headers onto a preflight reply.

    This middleware sits *outside* ``SecurityHeadersMiddleware``, and a
    preflight is answered here without ever calling through, so it would
    otherwise be the one response in the app carrying no security headers at
    all. The body is inert text a browser never renders, so this is
    consistency rather than a live exploit — but "every response carries
    them" is a far easier invariant to keep than a documented exception.
    """
    for key, value in SECURITY_HEADERS:
        response.headers[key.decode("latin-1")] = value.decode("latin-1")
    response.headers["content-security-policy"] = API_CSP


class TenantCORSMiddleware:
    """Reflects an ``Origin`` only when it is a domain of the request's own
    tenant, or an exact match in the static platform allowlist."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self._static = frozenset(settings.cors_origin_list)

    async def _is_allowed(self, scope: Scope, origin: str) -> bool:
        if origin in self._static:
            return True

        source = origin_host(origin)
        if not source:
            return False

        headers = Headers(scope=scope)
        target = strip_port(headers.get("host", ""))
        if not target:
            return False
        # Same host: same-origin in every practical sense (a differing scheme
        # or port still means the caller is the site this API serves).
        if source == target:
            return True

        try:
            resolver: TenantResolver = scope["app"].state.tenant_resolver
            requested = await resolver.resolve(target)
            if requested is None:
                return False
            candidate = await resolver.resolve(source)
        except Exception:
            # A resolver outage (or an app built without a lifespan, so with no
            # resolver at all) must not silently *widen* CORS, and must not
            # raise out of middleware either: deny, and let the request itself
            # fail on the tenant middleware's own terms.
            logger.warning("cors_resolve_failed", origin=origin)
            return False
        return candidate is not None and candidate.id == requested.id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if origin is None:
            await self.app(scope, receive, send)
            return

        allowed = await self._is_allowed(scope, origin)

        # A preflight is answered here and never forwarded: it carries no
        # credentials and no tenant expectation, and forwarding it would make
        # every OPTIONS hit the tenant middleware and the router for nothing.
        if scope["method"] == "OPTIONS" and "access-control-request-method" in headers:
            if not allowed:
                logger.info("cors_preflight_rejected", origin=origin)
                # 403 rather than a 200 with no CORS headers: the browser
                # blocks either way, but this is honest in the network log.
                response = PlainTextResponse("CORS origin not allowed", status_code=403)
                _apply_baseline_headers(response)
                await response(scope, receive, send)
                return
            response = PlainTextResponse("OK", status_code=200, headers=self._allow_headers(origin))
            response.headers["access-control-allow-methods"] = ALLOW_METHODS
            response.headers["access-control-allow-headers"] = ALLOW_HEADERS
            response.headers["access-control-max-age"] = PREFLIGHT_MAX_AGE
            _apply_baseline_headers(response)
            await response(scope, receive, send)
            return

        if not allowed:
            # Not an error: the request proceeds, the browser just refuses to
            # hand the response to the calling script (a non-browser client is
            # unaffected, exactly as CORS intends).
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                for key, value in self._allow_headers(origin).items():
                    response_headers.append(key, value)
                response_headers.append("access-control-expose-headers", EXPOSE_HEADERS)
            await send(message)

        await self.app(scope, receive, send_wrapper)

    @staticmethod
    def _allow_headers(origin: str) -> dict[str, str]:
        return {
            "access-control-allow-origin": origin,
            "access-control-allow-credentials": "true",
            # The response body differs per origin, so a shared cache must not
            # serve one tenant's response to another's origin.
            "vary": "Origin",
        }
