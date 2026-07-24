"""HTTP validator caching for public GETs (§11).

Two mechanisms, both aimed at letting a CDN and the client's own browser
absorb anonymous traffic instead of hitting the origin every time:

* ``Cache-Control: public, s-maxage=N`` — a shared cache (the CDN) may serve
  the response to *anyone* for ``N`` seconds. The window is deliberately short
  (§11: ~60s) so a published listing or edited page appears quickly; the point
  is to collapse a burst of identical anonymous reads, not to cache for hours.
* ``ETag`` + ``304 Not Modified`` — a conditional revalidation. The response
  carries a content-hash ``ETag``; a client that already has that version
  sends ``If-None-Match`` and gets an empty ``304`` back, saving the body
  transfer even after ``s-maxage`` expires.

The ETag is a **strong content hash** of the serialized body, so it is correct
regardless of how the value was produced (localisation, joins, aggregates) —
we never have to reason about which inputs changed. ``Last-Modified`` is an
optional secondary validator when a meaningful timestamp exists (a listing's
``updated_at``); a client may revalidate with either ``If-None-Match`` or
``If-Modified-Since``.

Only ever applied to **public, anonymous** GETs — a response that varies by
authenticated user or tenant-scoped ownership must not be marked
``public``/shared-cacheable, or one tenant's edge cache could serve another's
data. The public routers resolve the tenant from the Host, so the CDN keying
by Host keeps agencies separate; these helpers still set ``Vary`` accordingly.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any

from fastapi import Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel


def _etag_for(body: bytes) -> str:
    """A strong ETag: a quoted hex digest of the exact response body."""
    return f'"{hashlib.sha256(body).hexdigest()}"'


def _if_none_match_matches(header: str | None, etag: str) -> bool:
    """RFC 9110 ``If-None-Match``: ``*`` matches anything, else a comma list of
    entity-tags is compared **weakly** (a ``W/`` prefix is ignored for the
    comparison, which is what a validator match requires)."""
    if not header:
        return False
    header = header.strip()
    if header == "*":
        return True
    target = etag.lstrip("W/")
    return any(candidate.strip().lstrip("W/") == target for candidate in header.split(","))


def _not_modified_since(header: str | None, last_modified: datetime) -> bool:
    if not header:
        return False
    try:
        since = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return False
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    # HTTP dates have second resolution; truncate so sub-second drift doesn't
    # defeat a genuine match.
    return last_modified.replace(microsecond=0) <= since.replace(microsecond=0)


def apply_public_cache(
    request: Request,
    response: Response,
    *,
    s_maxage: int,
    last_modified: datetime | None = None,
) -> Response:
    """Attach validators + ``Cache-Control`` to a rendered public GET response,
    returning a ``304`` (with the validators, no body) when the client's
    conditional request already matches.

    ``response`` must already carry the serialized body (call this *after* the
    handler has built the ``Response``/model has been rendered). Returns the
    response to send — either the original with headers added, or a fresh
    ``304``.
    """
    etag = _etag_for(bytes(response.body))
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = f"public, s-maxage={s_maxage}"
    # The public routers key by Host; make that explicit so a shared cache
    # never serves one agency's response for another's Origin/Host.
    response.headers["Vary"] = "Accept-Language, Origin"
    if last_modified is not None:
        if last_modified.tzinfo is None:
            last_modified = last_modified.replace(tzinfo=UTC)
        response.headers["Last-Modified"] = format_datetime(last_modified, usegmt=True)

    if _if_none_match_matches(request.headers.get("if-none-match"), etag) or (
        last_modified is not None
        and _not_modified_since(request.headers.get("if-modified-since"), last_modified)
    ):
        not_modified = Response(status_code=304)
        # A 304 must carry the same validators so the client can revalidate
        # again without another full fetch.
        for name in ("ETag", "Cache-Control", "Vary", "Last-Modified"):
            if name in response.headers:
                not_modified.headers[name] = response.headers[name]
        return not_modified

    return response


def cached_json_response(
    request: Request,
    model: BaseModel | list[BaseModel],
    *,
    s_maxage: int,
    last_modified: datetime | None = None,
) -> Response:
    """Render an ``OutSchema`` model (or list of them) to a camelCase JSON
    response, then attach validators + ``Cache-Control`` and short-circuit to a
    ``304`` when the client's conditional request matches (:func:`apply_public_cache`).

    Serialization goes through ``model_dump(by_alias=True)`` so the body is
    byte-identical to what FastAPI's ``response_model`` would emit (the ETag
    must hash exactly what the client receives). The endpoint's declared return
    type becomes ``Response`` — FastAPI then skips its own model coercion.
    """
    payload: Any
    if isinstance(model, list):
        payload = [m.model_dump(by_alias=True) for m in model]
    else:
        payload = model.model_dump(by_alias=True)
    response = JSONResponse(content=jsonable_encoder(payload))
    return apply_public_cache(request, response, s_maxage=s_maxage, last_modified=last_modified)


__all__ = ["apply_public_cache", "cached_json_response"]
