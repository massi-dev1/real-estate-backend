"""transactions: back-office deals, milestones & documents (§8.13, tenant RLS)

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-21

Three tenant-owned, RLS-protected tables (§8.13):
- ``deals`` — the deal record once a lead converts. ``listing_id``/``lead_id``/
  ``contact_id`` link into other modules **by column only** (same stance as
  valuations/appointments — no cross-module model import). Money follows §9:
  ``price``/``commission_amount`` are ``Numeric(14,2)`` with a sibling
  ``currency`` (matching listings' ``price``), never floats. ``owner_user_id``
  is the deal's owning agent — visibility scoping (§8.5) keys on it.
- ``deal_milestones`` — checklist items (title, due_date, owner_user_id,
  completed_at). Seeded from a template on deal create or added ad hoc.
- ``deal_documents`` — private-bucket object key + server-computed sha256,
  uploaded_by, doc_type, and the **e-signature seam** columns
  (``signature_status``/``signature_request_id``) — the adapter interface is
  designed, the provider integration deferred (no creds this part).

Status/basis/doc-status/signature-status are non-native check-constrained
varchars (the codebase's standard — no Postgres enum type to migrate).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEAL_STATUS = sa.Enum(
    "open",
    "under_contract",
    "closed_won",
    "closed_lost",
    name="deal_status",
    native_enum=False,
    length=40,
)
COMMISSION_BASIS = sa.Enum(
    "percentage",
    "flat",
    name="commission_basis",
    native_enum=False,
    length=40,
)
DOC_STATUS = sa.Enum(
    "pending",
    "ready",
    "failed",
    name="deal_document_status",
    native_enum=False,
    length=40,
)
SIGNATURE_STATUS = sa.Enum(
    "none",
    "requested",
    "signed",
    "declined",
    name="deal_signature_status",
    native_enum=False,
    length=40,
)

_TENANT_TABLES = ("deals", "deal_milestones", "deal_documents")


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def upgrade() -> None:
    op.create_table(
        "deals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("status", DEAL_STATUS, nullable=False),
        # Column-only CRM links (no cross-module model import).
        sa.Column("listing_id", sa.Uuid(), nullable=True),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("contact_id", sa.Uuid(), nullable=True),
        # Money (§9): string-amount on the wire, Numeric at rest, never float.
        sa.Column("price", sa.Numeric(14, 2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="DZD"),
        sa.Column("commission_basis", COMMISSION_BASIS, nullable=True),
        sa.Column("commission_rate", sa.Numeric(6, 3), nullable=True),
        sa.Column("commission_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("lost_reason", sa.String(length=500), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "commission_rate IS NULL OR (commission_rate >= 0 AND commission_rate <= 100)",
            name="ck_deals_commission_rate_range",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_deals_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_deals_owner_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["listings.id"],
            name=op.f("fk_deals_listing_id_listings"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"],
            ["leads.id"],
            name=op.f("fk_deals_lead_id_leads"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name=op.f("fk_deals_contact_id_contacts"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deals")),
    )
    op.create_index(op.f("ix_deals_tenant_id"), "deals", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_deals_owner_user_id"), "deals", ["owner_user_id"], unique=False)
    # Covering index for the portal list (scope by owner, filter by status,
    # keyset on created_at).
    op.create_index(
        "ix_deals_tenant_owner_status_created",
        "deals",
        ["tenant_id", "owner_user_id", "status", "created_at"],
        unique=False,
    )

    op.create_table(
        "deal_milestones",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("deal_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
        # Idempotency stamp for the due-milestone reminder sweep (same stance as
        # appointments' reminder_*_sent_at / listings.stale_flagged_at).
        sa.Column("reminder_sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_deal_milestones_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["deal_id"],
            ["deals.id"],
            name=op.f("fk_deal_milestones_deal_id_deals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_deal_milestones_owner_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deal_milestones")),
    )
    op.create_index(
        op.f("ix_deal_milestones_tenant_id"), "deal_milestones", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_deal_milestones_deal_id"), "deal_milestones", ["deal_id"], unique=False
    )
    # The reminder sweep scans open, uncompleted, unreminded milestones with a
    # due_date across a tenant.
    op.create_index(
        "ix_deal_milestones_tenant_due_pending",
        "deal_milestones",
        ["tenant_id", "due_date", "completed_at"],
        unique=False,
    )

    op.create_table(
        "deal_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("deal_id", sa.Uuid(), nullable=False),
        sa.Column("doc_type", sa.String(length=60), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=120), nullable=False),
        sa.Column("storage_key", sa.String(length=500), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("status", DOC_STATUS, nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=True),
        # E-signature seam (§8.13): columns + adapter interface shipped; the
        # actual provider integration is deferred (design the seam, defer the
        # provider — same stance as Part 20's portal adapter).
        sa.Column("signature_status", SIGNATURE_STATUS, nullable=False, server_default="none"),
        sa.Column("signature_request_id", sa.String(length=255), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_deal_documents_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["deal_id"],
            ["deals.id"],
            name=op.f("fk_deal_documents_deal_id_deals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"],
            ["users.id"],
            name=op.f("fk_deal_documents_uploaded_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deal_documents")),
    )
    op.create_index(
        op.f("ix_deal_documents_tenant_id"), "deal_documents", ["tenant_id"], unique=False
    )
    op.create_index(op.f("ix_deal_documents_deal_id"), "deal_documents", ["deal_id"], unique=False)

    for table in _TENANT_TABLES:
        for stmt in enable_tenant_rls_sql(table):
            op.execute(stmt)


def downgrade() -> None:
    for table in _TENANT_TABLES:
        for stmt in disable_tenant_rls_sql(table):
            op.execute(stmt)

    op.drop_index(op.f("ix_deal_documents_deal_id"), table_name="deal_documents")
    op.drop_index(op.f("ix_deal_documents_tenant_id"), table_name="deal_documents")
    op.drop_table("deal_documents")

    op.drop_index("ix_deal_milestones_tenant_due_pending", table_name="deal_milestones")
    op.drop_index(op.f("ix_deal_milestones_deal_id"), table_name="deal_milestones")
    op.drop_index(op.f("ix_deal_milestones_tenant_id"), table_name="deal_milestones")
    op.drop_table("deal_milestones")

    op.drop_index("ix_deals_tenant_owner_status_created", table_name="deals")
    op.drop_index(op.f("ix_deals_owner_user_id"), table_name="deals")
    op.drop_index(op.f("ix_deals_tenant_id"), table_name="deals")
    op.drop_table("deals")
