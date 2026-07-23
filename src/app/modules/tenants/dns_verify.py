"""DNS TXT-record verification for custom domains (§8.16).

A tenant proves control of a domain by publishing the domain's
``verification_token`` as a TXT record; the platform confirms it here. This is
the *data model + API* for the challenge — the actual on-demand-TLS provisioning
(Caddy on-demand-TLS / Cloudflare-for-SaaS) is ops-side work that consumes
``tenant_domains.verified_at`` once this flips it, and is out of scope for the
FastAPI app.

The TXT lookup is wrapped in an injectable async callable so the verify path is
testable without real DNS (the test suite passes a stub resolver). The default
uses ``dnspython`` and degrades to "no records" on any resolution error — a
failure to resolve is a failed verification, never a 500.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog

logger = structlog.get_logger(__name__)

# domain → the list of TXT record strings currently published for it.
TxtResolver = Callable[[str], Awaitable[list[str]]]


async def default_txt_lookup(domain: str) -> list[str]:
    """Resolve TXT records for ``domain`` via dnspython, off the event loop.

    Any resolution failure (NXDOMAIN, timeout, no TXT set) degrades to an empty
    list — a domain that cannot be resolved simply fails verification."""

    def _lookup() -> list[str]:
        import dns.resolver

        try:
            answers = dns.resolver.resolve(domain, "TXT")
        except Exception:
            return []
        records: list[str] = []
        for rdata in answers:
            # A TXT rdata is one or more quoted strings; join the byte chunks.
            chunks = getattr(rdata, "strings", None)
            if chunks is not None:
                records.append(b"".join(chunks).decode("utf-8", "replace"))
            else:
                records.append(str(rdata).strip('"'))
        return records

    try:
        return await asyncio.to_thread(_lookup)
    except Exception:
        logger.warning("txt_lookup_failed", domain=domain)
        return []


async def txt_record_present(
    domain: str, expected: str, *, resolver: TxtResolver = default_txt_lookup
) -> bool:
    """Whether ``expected`` appears among the domain's published TXT records."""
    records = await resolver(domain)
    return any(expected == record.strip() for record in records)
