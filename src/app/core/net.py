"""SSRF guard for user-supplied outbound URLs (§10.4).

Anything that fetches or delivers to a URL a *tenant* controls — outbound
webhook endpoints (§8.14), and any future "import from URL" feature — must pass
through here first. A webhook target the platform will POST to, on a schedule,
signed with a secret, is a textbook server-side-request-forgery vector: point it
at ``http://169.254.169.254/`` (the cloud metadata service), ``http://localhost``
(an internal admin port), or a ``10.0.0.0/8`` service and the platform becomes a
confused deputy reaching places the tenant never could.

Two checks, and both matter:

1. **At registration** (``validate_public_url``) — reject an obviously-internal
   target loudly, so a misconfiguration surfaces as a 4xx to the admin, not as a
   silent never-delivering endpoint.
2. **At delivery** (``resolve_public_host`` / :class:`SsrfProtectedTransport`) —
   re-resolve the host and re-check *every* resolved address immediately before
   connecting, because DNS is mutable: a name that resolved to a public IP at
   registration can be re-pointed at ``127.0.0.1`` later (DNS rebinding). The
   httpx transport below also refuses to *follow* a redirect into a private
   range, closing the "public 302 → internal Location" bypass.

The private/loopback/link-local/reserved determination is delegated wholesale to
:mod:`ipaddress` (``is_global`` is the inverse of everything we want to block:
private, loopback, link-local, multicast, reserved, unspecified) rather than a
hand-maintained CIDR list that would inevitably miss a range.

``allow_private_hosts`` (config, default **off**) is the single escape hatch: the
test suite and local dev deliver to a mock webhook on ``127.0.0.1``, which the
guard would otherwise correctly refuse. Same offline-safe-default stance as the
portal/billing stubs — secure unless explicitly opened.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx


class SsrfError(Exception):
    """A URL was rejected as unsafe to fetch/deliver to (§10.4).

    Not an :class:`~app.core.exceptions.AppError`: this is raised from both a
    request path (endpoint registration → the router maps it to a 422) and a
    worker path (delivery → the caller records a failed delivery), so it stays a
    plain exception each caller translates in its own context.
    """


_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _addr_is_blocked(ip: str) -> bool:
    """True if ``ip`` is anything but a globally-routable public address.

    ``is_global`` is False for private, loopback, link-local, multicast,
    reserved and unspecified addresses — exactly the set an SSRF guard must
    refuse — so its negation is the whole blocklist, IPv4 and IPv6 alike.
    """
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return True  # not an address we can reason about — refuse
    # IPv4-mapped IPv6 (``::ffff:127.0.0.1``) would report is_global on the v6
    # wrapper while pointing at a blocked v4 host; unwrap and re-check.
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return not parsed.is_global


def _resolve_addresses(host: str) -> list[str]:
    """All A/AAAA addresses ``host`` currently resolves to (empty on failure)."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    # sockaddr[0] is the address; str for AF_INET/AF_INET6 (the only families
    # getaddrinfo returns for an IP host), typed str|int by the stubs.
    return [str(info[4][0]) for info in infos]


def validate_public_url(url: str, *, allow_private_hosts: bool = False) -> None:
    """Reject a URL that is malformed, non-http(s), or resolves to a non-public
    address. Raises :class:`SsrfError`; returns ``None`` when the URL is safe.

    ``allow_private_hosts`` bypasses the address check (dev/test against a local
    mock) but never the scheme/host structural checks — a garbage URL is still
    rejected."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise SsrfError(f"URL scheme must be http or https, got {parsed.scheme!r}.")
    host = parsed.hostname
    if not host:
        raise SsrfError("URL has no host.")
    if allow_private_hosts:
        return
    # A literal IP in the URL is checked directly; a name is resolved and every
    # address it maps to must be public (a single blocked answer is enough to
    # refuse — an attacker controls their own DNS).
    literal = _literal_ip(host)
    addresses = [literal] if literal is not None else _resolve_addresses(host)
    if not addresses:
        raise SsrfError(f"Host {host!r} does not resolve.")
    for addr in addresses:
        if _addr_is_blocked(addr):
            raise SsrfError(f"Host {host!r} resolves to a non-public address ({addr}).")


def _literal_ip(host: str) -> str | None:
    """Return ``host`` if it is already an IP literal, else None. A bracketed
    IPv6 host from ``urlparse`` (``[::1]``) arrives without brackets already."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return host


class SsrfProtectedTransport(httpx.AsyncBaseTransport):
    """An httpx transport that resolves the target host **once**, validates that
    exact address, and then **pins the connection to it** — on every request,
    including each hop of a redirect chain.

    Pinning closes the TOCTOU/DNS-rebinding window a validate-then-let-httpx-
    re-resolve design leaves open: if the guard resolves a name to a public IP
    but httpx performs its *own* second lookup to open the socket, a short-TTL /
    round-robin attacker DNS can return an internal address for that second
    lookup — so httpx would connect to exactly the host the guard just approved a
    *different* answer for. Here the request URL's host is rewritten to the
    validated IP literal, the original hostname is preserved in the ``Host``
    header (and as the TLS SNI / cert-check name), so the socket connects to the
    one address that was checked — no second resolution happens.

    Wraps a real transport; the guard runs first and raises :class:`SsrfError`
    before any socket is opened."""

    def __init__(self, inner: httpx.AsyncBaseTransport, *, allow_private_hosts: bool) -> None:
        self._inner = inner
        self._allow = allow_private_hosts

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if self._allow:
            # Escape hatch (dev/test to a local mock): scheme/host structure is
            # still enforced, but no address check or pinning.
            validate_public_url(str(request.url), allow_private_hosts=True)
            return await self._inner.handle_async_request(request)

        # Resolve once, validate that single address, connect to *it*.
        literal = _literal_ip(host)
        addresses = [literal] if literal is not None else _resolve_addresses(host)
        if not addresses:
            raise SsrfError(f"Host {host!r} does not resolve.")
        pinned = addresses[0]
        if _addr_is_blocked(pinned):
            raise SsrfError(f"Host {host!r} resolves to a non-public address ({pinned}).")
        if request.url.scheme not in _ALLOWED_SCHEMES:
            raise SsrfError(f"URL scheme must be http or https, got {request.url.scheme!r}.")

        # Pin: connect to the validated IP, keep the original hostname for the
        # Host header + TLS SNI so cert validation and vhost routing still work.
        pinned_url = request.url.copy_with(host=pinned)
        pinned_request = httpx.Request(
            method=request.method,
            url=pinned_url,
            headers=request.headers,  # carries the original Host header
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": host},
        )
        return await self._inner.handle_async_request(pinned_request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def build_guarded_client(*, allow_private_hosts: bool, timeout: float) -> httpx.AsyncClient:
    """An httpx client that re-checks every request/redirect against the SSRF
    guard. Redirects are *followed* (a webhook receiver may legitimately 301 to
    a canonical path) but each hop is re-validated by the transport, so a
    redirect into a private range raises rather than connecting."""
    transport = SsrfProtectedTransport(
        httpx.AsyncHTTPTransport(), allow_private_hosts=allow_private_hosts
    )
    return httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=True)


__all__ = [
    "SsrfError",
    "SsrfProtectedTransport",
    "build_guarded_client",
    "validate_public_url",
]
