"""Tenant data export for offboarding (§8.16).

The offboard flow exports the tenant's data to a downloadable archive before the
scheduled purge. This is the **straightforward "dump the tenant's rows to a JSON
archive"** version the part brief calls for; aligning it with the compliance
DSR-export shape (§10.12, Part 23) is noted as a later reconciliation item —
Part 23's per-user ``GET /me/export`` fans out through each module's service,
whereas this whole-tenant dump reads the tenant-owned tables directly under the
tenant RLS GUC (a full-tenant admin export, not a per-subject one).

Runs in a Celery task with the tenant GUC set (so RLS scopes every SELECT to the
tenant), serialises each table to JSON, and uploads a single archive object to
the private bucket; the offboard record keeps the object key for a presigned
download.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import Base
from app.core.storage import ObjectStorage

# The tenant-owned tables dumped in the archive. Kept as an explicit allowlist
# (not "every table with a tenant_id") so adding a module is a deliberate export
# decision, and platform/global tables never leak into a tenant's export.
_EXPORT_TABLES: tuple[str, ...] = (
    "listings",
    "listing_status_history",
    "listing_media",
    "leads",
    "contacts",
    "lead_activities",
    "agent_profiles",
    "teams",
    "team_members",
    "appointments",
    "agent_availability",
    "valuation_requests",
    "content_pages",
    "legal_pages",
    "blog_posts",
    "blog_categories",
    "neighborhood_guides",
    "market_reports",
    "reviews",
    "deals",
    "deal_milestones",
    "deal_documents",
    "favorites",
    "saved_searches",
    "notifications",
)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)


async def _dump_table(session: AsyncSession, table: Table) -> list[dict[str, Any]]:
    rows = (await session.execute(select(table))).mappings().all()
    dumped: list[dict[str, Any]] = []
    for row in rows:
        record: dict[str, Any] = {}
        for key, val in row.items():
            # PostGIS geometry columns don't JSON-serialise; skip them (the
            # archive is a data dump, not a GIS export — lat/lng live in address).
            if type(val).__name__ in {"WKBElement", "WKTElement"}:
                continue
            record[key] = val
        dumped.append(record)
    return dumped


async def export_tenant_data(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    storage: ObjectStorage,
) -> str:
    """Dump the tenant's rows to a JSON archive in the private bucket and return
    the object key. The session must already be scoped to ``tenant_id`` (RLS
    GUC set) so every SELECT sees only this tenant's data."""
    metadata_tables = Base.metadata.tables
    archive: dict[str, Any] = {
        "tenant_id": str(tenant_id),
        "exported_at": datetime.now(UTC).isoformat(),
        "tables": {},
    }
    for name in _EXPORT_TABLES:
        table = metadata_tables.get(name)
        if table is None:
            continue
        archive["tables"][name] = await _dump_table(session, table)

    body = json.dumps(archive, default=_json_default, ensure_ascii=False).encode("utf-8")
    key = f"tenants/{tenant_id}/exports/{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    storage.put_object(storage.docs_bucket, key, body, "application/json")
    return key
