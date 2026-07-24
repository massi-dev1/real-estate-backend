"""Pure-ASGI middleware: request context (request-id + access log) and security headers.

Written as raw ASGI (not ``BaseHTTPMiddleware``) to avoid its per-request
overhead and streaming pitfalls.
"""

import time
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings

logger = structlog.get_logger("app.access")

SECURITY_HEADERS: list[tuple[bytes, bytes]] = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]

# The only HTML this API serves is Swagger UI at /docs (disabled in
# production), so the policy is default-deny plus exactly what Swagger needs:
# its bundle from the jsdelivr CDN FastAPI points at, and the inline
# style/script it injects to boot itself. No `frame-ancestors` beyond 'none'
# (belt-and-braces with X-Frame-Options), no form posts, no plugins.
DOCS_CSP = (
    "default-src 'none'; "
    "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "img-src 'self' https://fastapi.tiangolo.com data:; "
    "font-src 'self' https://cdn.jsdelivr.net; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

# Every JSON response gets the strictest possible policy: a problem+json or an
# API payload has nothing to load, and a browser that is tricked into
# rendering one as HTML must not be able to fetch anything.
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"

_DOCS_PATHS: tuple[str, ...] = ("/docs", "/redoc", "/openapi.json")


class RequestContextMiddleware:
    """Binds request_id/method/path into structlog contextvars, echoes
    ``X-Request-ID`` on the response, and emits one access-log line."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        request_id = headers.get(b"x-request-id", b"").decode("latin-1") or uuid.uuid4().hex

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id, method=scope["method"], path=scope["path"]
        )

        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message.setdefault("headers", []).append(
                    (b"x-request-id", request_id.encode("latin-1"))
                )
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.info("request", status=status_code, duration_ms=duration_ms)
            structlog.contextvars.clear_contextvars()


class SecurityHeadersMiddleware:
    """Adds the baseline security headers, HSTS and a CSP (§10.1) to every
    HTTP response.

    HSTS is deliberately conditional. Caddy already sets it at the edge
    (§16), but a header set here too survives a proxy misconfiguration and
    covers any deployment that fronts the app differently — defence in depth.
    It is emitted only when the deployment is TLS-terminated (staging /
    production) or the request itself arrived over https, because a
    ``max-age`` cached from plain-http local dev would pin ``localhost`` to
    https in the developer's browser for a year.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self._tls_deployment = settings.app_env in ("staging", "production")
        directive = f"max-age={settings.hsts_max_age_seconds}"
        if settings.hsts_include_subdomains:
            directive += "; includeSubDomains"
        self._hsts = directive.encode("latin-1")

    def _headers_for(self, scope: Scope) -> list[tuple[bytes, bytes]]:
        headers = list(SECURITY_HEADERS)
        path = scope.get("path", "")
        csp = DOCS_CSP if path.startswith(_DOCS_PATHS) else API_CSP
        headers.append((b"content-security-policy", csp.encode("latin-1")))
        if self._tls_deployment or scope.get("scheme") == "https":
            headers.append((b"strict-transport-security", self._hsts))
        return headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        extra = self._headers_for(scope)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend(extra)
            await send(message)

        await self.app(scope, receive, send_wrapper)
