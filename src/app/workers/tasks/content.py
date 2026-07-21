"""Content-CMS worker tasks (§8.10 slice 3, §12).

``recompute_guide_stats`` — nightly Beat job on the ``analytics`` queue (batch,
no human-facing latency, same class as ``flag_stale_listings`` and blog's
scheduled-publish sweep). Per active tenant (``run_scoped_many``), for every
published guide with a boundary it recomputes ``listing_count`` and
``median_price`` from the published listings whose point falls inside the
polygon (``ST_Contains`` + ``percentile_cont(0.5)`` in Postgres, not app-side).
Guides with no boundary get no auto stats. Idempotent: a re-run simply
recomputes the same numbers.

``generate_report_pdf`` — enqueued post-commit when a market report is
published (queue ``media`` — it's a rendering job, same class as image
processing). It renders the author-supplied stats into a PDF via reportlab
(pure Python, no headless browser) and uploads it to the private bucket, then
flips the row to ``ready`` so the gated download can serve it. Idempotent: the
key is deterministic and the status guard skips a row that's no longer
``published``.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from io import BytesIO
from typing import Any

import structlog
from botocore.exceptions import ClientError
from celery import shared_task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.i18n import DEFAULT_LOCALE, pick_localized
from app.core.storage import create_storage
from app.modules.content.models import MarketReport, NeighborhoodGuide, ReportStatus
from app.modules.content.repository import ContentRepository
from app.modules.listings.service import get_listing_service
from app.modules.tenants.models import Tenant, TenantStatus
from app.workers.db import run_scoped, run_scoped_many

logger = structlog.get_logger(__name__)


# ---- guide stats sweep ----


async def _active_tenant_ids(session: AsyncSession) -> list[uuid.UUID]:
    stmt = select(Tenant.id).where(Tenant.status != TenantStatus.SUSPENDED)
    return list((await session.execute(stmt)).scalars())


async def _recompute_tenant_guides(
    session: AsyncSession, tenant_id: uuid.UUID, now: datetime
) -> int:
    repo = ContentRepository(session)
    listings = get_listing_service(session)
    guides = await repo.published_guides_with_boundary(tenant_id)
    for guide in guides:
        rings = _guide_boundary_rings(guide)
        if not rings:
            continue
        count, median = await listings.boundary_stats(tenant_id, boundary_rings=rings)
        guide.stats = {
            "listing_count": count,
            "median_price": str(median) if median is not None else None,
        }
        guide.stats_computed_at = now
    return len(guides)


def _guide_boundary_rings(guide: NeighborhoodGuide) -> list[list[tuple[float, float]]] | None:
    from app.common.geo import multipolygon_rings

    return multipolygon_rings(guide.boundary)


@shared_task(name="app.workers.tasks.content.recompute_guide_stats")
def recompute_guide_stats() -> int:
    """Idempotent: recomputes the same aggregates on every run; a re-run or an
    overlapping run just overwrites ``stats`` with identical numbers."""
    now = datetime.now(UTC)

    async def _list_tenants(session: AsyncSession) -> list[uuid.UUID]:
        return await _active_tenant_ids(session)

    tenant_ids = run_scoped(None, _list_tenants)

    calls: list[tuple[uuid.UUID | None, Callable[[AsyncSession], Awaitable[int]]]] = [
        (tid, partial(_recompute_tenant_guides, tenant_id=tid, now=now)) for tid in tenant_ids
    ]
    counts = run_scoped_many(calls)

    total = 0
    for tenant_id, count in zip(tenant_ids, counts, strict=True):
        total += count
        if count:
            logger.info("guide_stats_recomputed", tenant_id=str(tenant_id), count=count)
    return total


# ---- market-report PDF render ----


async def _load_report(session: AsyncSession, report_id: uuid.UUID) -> MarketReport | None:
    stmt = select(MarketReport).where(MarketReport.id == report_id)
    return (await session.execute(stmt)).scalar_one_or_none()


def _render_report_pdf(title: str, stats: dict[str, Any]) -> bytes:
    """A minimal, self-contained stats PDF — no headless browser, no HTML
    engine. reportlab lays out the title and a flat key/value table of the
    author-supplied numbers; nested structures are rendered as their string
    form so an arbitrary stats blob never crashes the render."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, title=title)
    styles = getSampleStyleSheet()
    story: list[Any] = [Paragraph(title or "Market Report", styles["Title"]), Spacer(1, 0.6 * cm)]

    rows = [(str(k), _stat_cell(v)) for k, v in stats.items()]
    if rows:
        table = Table([("Metric", "Value"), *rows], colWidths=[8 * cm, 8 * cm])
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.white, colors.HexColor("#f3f4f6")],
                    ),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("PADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.append(table)
    else:
        story.append(Paragraph("No data supplied.", styles["Normal"]))

    doc.build(story)
    return buffer.getvalue()


def _stat_cell(value: Any) -> str:
    if isinstance(value, dict | list):
        return str(value)
    if isinstance(value, float | Decimal):
        return f"{value:g}" if isinstance(value, float) else str(value)
    return str(value)


@shared_task(
    name="app.workers.tasks.content.generate_report_pdf",
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=120,
)
def generate_report_pdf(report_id: str, tenant_id: str) -> str:
    settings = get_settings()
    storage = create_storage(settings)
    tid = uuid.UUID(tenant_id)
    rid = uuid.UUID(report_id)

    async def _snapshot(session: AsyncSession) -> tuple[str, dict[str, Any]] | None:
        report = await _load_report(session, rid)
        if report is None or report.status is not ReportStatus.PUBLISHED:
            return None  # deleted, unpublished, or already rendered
        return pick_localized(report.title, DEFAULT_LOCALE) or "Market Report", report.stats

    snapshot = run_scoped(tid, _snapshot)
    if snapshot is None:
        return "skipped"
    title, stats = snapshot

    pdf = _render_report_pdf(title, stats)
    key = f"tenants/{tenant_id}/reports/{report_id}/report.pdf"
    storage.put_object(storage.docs_bucket, key, pdf, "application/pdf")

    async def _mark_ready(session: AsyncSession) -> bool:
        report = await _load_report(session, rid)
        if report is None or report.status is not ReportStatus.PUBLISHED:
            return False
        report.pdf_object_key = key
        report.status = ReportStatus.READY
        report.generated_at = datetime.now(UTC)
        return True

    if not run_scoped(tid, _mark_ready):
        # The row was deleted or unpublished mid-render — clean up our upload.
        try:
            storage.delete_objects(storage.docs_bucket, [key])
        except ClientError:
            logger.warning("report_pdf_orphan_cleanup_failed", report_id=report_id)
        return "skipped"

    logger.info("market_report_pdf_generated", report_id=report_id, bytes=len(pdf))
    return "ready"
