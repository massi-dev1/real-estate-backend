"""Pydantic schemas for the compliance module (§8.17)."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.core.schema import InputSchema, OutSchema
from app.modules.compliance.models import ConsentCategory, DsrKind, DsrStatus

# ---- consent records ----


class ConsentIn(InputSchema):
    """Public cookie-banner submission — the visitor's per-category choices.

    Anonymous by design: the subject is identified by ``session_id`` (a cookie
    id) when there's no account. An authenticated caller is identified by the
    bearer token instead, so ``session_id`` is optional either way, but at least
    one identity must resolve (the service rejects a fully-anonymous submission
    with no session id)."""

    session_id: str | None = Field(default=None, max_length=64)
    # Per-category grant flags — every category the banner offered.
    choices: dict[ConsentCategory, bool]


class ConsentRecordOut(OutSchema):
    id: uuid.UUID
    category: ConsentCategory
    granted: bool
    source: str
    legal_page_id: uuid.UUID | None
    legal_version: int | None
    created_at: datetime


# ---- cookie-consent config (portal) ----


class CookieConsentConfigIn(InputSchema):
    """Full-replacement PUT of the tenant's cookie-banner config. Categories are
    envelope-validated (a known category key + a dict payload) — the frontend
    owns the exact copy shape, like content page blocks."""

    categories: list[dict[str, Any]] = Field(default_factory=list)
    banner_copy: dict[str, Any] = Field(default_factory=dict)
    is_enabled: bool = True


class CookieConsentConfigOut(OutSchema):
    categories: list[dict[str, Any]]
    banner_copy: dict[str, Any]
    is_enabled: bool


# ---- data-subject requests (§10.12) ----


class DsrRequestOut(OutSchema):
    id: uuid.UUID
    kind: DsrKind
    status: DsrStatus
    purge_scheduled_at: datetime | None
    completed_at: datetime | None
    result: dict[str, Any]
    created_at: datetime


class ErasureAck(OutSchema):
    """202-style ack for ``DELETE /me``: the account is soft-deleted now and the
    hard purge is scheduled (§10.12)."""

    request_id: uuid.UUID
    purge_scheduled_at: datetime


# ---- DSR export (fan-out aggregate, §10.12) ----


class DataExportOut(OutSchema):
    """The subject's data aggregated read-only across every module that holds a
    ``user_id`` / ``contact_id`` for them. Each ``sections`` entry is one
    module's own boundary-method dump — never a raw cross-module table read."""

    subject_user_id: uuid.UUID
    subject_email: str | None
    exported_at: datetime
    sections: dict[str, Any]
