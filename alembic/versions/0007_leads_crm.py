"""leads, contacts, activities, assignment rules, drip state (tenant RLS)

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-16

All five tables are strictly tenant-owned (NOT NULL ``tenant_id``) and get the
fail-closed tenant RLS policy. ``assignment_rules`` and ``lead_drip_state`` are
one-row-per-tenant / one-row-per-lead policy tables, upserted atomically by the
service layer. Deferred to later parts: territory-based assignment (needs
§8.5's agent_profiles/service_areas, which don't exist yet), SMS drip steps
(no SMS adapter exists yet).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEAD_SOURCE = sa.Enum(
    "listing_form",
    "valuation",
    "search_signup",
    "chat",
    "phone",
    "portal",
    "ad",
    "other",
    name="lead_source",
    native_enum=False,
    length=20,
)

LEAD_STAGE = sa.Enum(
    "new",
    "contacted",
    "qualified",
    "touring",
    "offer",
    "won",
    "lost",
    name="lead_stage",
    native_enum=False,
    length=20,
)

ACTIVITY_TYPE = sa.Enum(
    "note",
    "call",
    "email",
    "sms",
    "status_change",
    "assignment",
    "tour",
    "system",
    name="lead_activity_type",
    native_enum=False,
    length=20,
)

ASSIGNMENT_STRATEGY = sa.Enum(
    "listing_agent",
    "round_robin",
    "territory",
    name="assignment_strategy",
    native_enum=False,
    length=20,
)

DRIP_STOP_REASON = sa.Enum(
    "stage_advanced",
    "replied",
    "sequence_complete",
    "manual",
    name="drip_stop_reason",
    native_enum=False,
    length=20,
)


def upgrade() -> None:
    op.create_table(
        "contacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("first_name", sa.String(length=80), nullable=True),
        sa.Column("last_name", sa.String(length=80), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("whatsapp", sa.String(length=32), nullable=True),
        sa.Column(
            "consent", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "tags", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("notes", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_contacts_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contacts")),
    )
    op.create_index(op.f("ix_contacts_tenant_id"), "contacts", ["tenant_id"], unique=False)
    # Expression index: the dedupe lookup filters on lower(email), which a
    # plain (tenant_id, email) btree can't serve (review finding).
    op.create_index(
        "ix_contacts_tenant_email",
        "contacts",
        ["tenant_id", sa.text("lower(email)")],
        unique=False,
    )
    op.create_index("ix_contacts_tenant_phone", "contacts", ["tenant_id", "phone"], unique=False)

    op.create_table(
        "leads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("contact_id", sa.Uuid(), nullable=False),
        sa.Column("listing_id", sa.Uuid(), nullable=True),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column("source", LEAD_SOURCE, nullable=False),
        sa.Column(
            "source_meta",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("stage", LEAD_STAGE, server_default="new", nullable=False),
        sa.Column("score", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("lost_reason", sa.String(length=200), nullable=True),
        sa.Column("first_response_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_leads_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name=op.f("fk_leads_contact_id_contacts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["listings.id"],
            name=op.f("fk_leads_listing_id_listings"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["users.id"],
            name=op.f("fk_leads_agent_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leads")),
    )
    op.create_index(op.f("ix_leads_tenant_id"), "leads", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_leads_contact_id"), "leads", ["contact_id"], unique=False)
    op.create_index(op.f("ix_leads_listing_id"), "leads", ["listing_id"], unique=False)
    op.create_index(
        "ix_leads_tenant_stage_created",
        "leads",
        ["tenant_id", "stage", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_leads_tenant_agent_stage",
        "leads",
        ["tenant_id", "agent_id", "stage"],
        unique=False,
    )

    op.create_table(
        "lead_activities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("type", ACTIVITY_TYPE, nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
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
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_lead_activities_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"],
            ["leads.id"],
            name=op.f("fk_lead_activities_lead_id_leads"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_lead_activities_actor_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lead_activities")),
    )
    op.create_index(
        op.f("ix_lead_activities_tenant_id"), "lead_activities", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_lead_activities_lead_created",
        "lead_activities",
        ["lead_id", sa.text("created_at DESC")],
        unique=False,
    )

    op.create_table(
        "assignment_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("strategy", ASSIGNMENT_STRATEGY, server_default="listing_agent", nullable=False),
        sa.Column(
            "config", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
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
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_assignment_rules_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assignment_rules")),
        # One assignment policy per tenant; also serves as the tenant_id index.
        sa.UniqueConstraint("tenant_id", name=op.f("uq_assignment_rules_tenant_id")),
    )

    op.create_table(
        "lead_drip_state",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("step_index", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("next_send_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("stopped_reason", DRIP_STOP_REASON, nullable=True),
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
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_lead_drip_state_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"],
            ["leads.id"],
            name=op.f("fk_lead_drip_state_lead_id_leads"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lead_drip_state")),
        sa.UniqueConstraint("lead_id", name=op.f("uq_lead_drip_state_lead_id")),
    )
    op.create_index(
        op.f("ix_lead_drip_state_tenant_id"), "lead_drip_state", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_lead_drip_state_due",
        "lead_drip_state",
        ["tenant_id", "next_send_at"],
        unique=False,
        postgresql_where=sa.text("stopped_at IS NULL"),
    )

    for table in ("contacts", "leads", "lead_activities", "assignment_rules", "lead_drip_state"):
        for stmt in enable_tenant_rls_sql(table):
            op.execute(stmt)


def downgrade() -> None:
    for table in ("lead_drip_state", "assignment_rules", "lead_activities", "leads", "contacts"):
        for stmt in disable_tenant_rls_sql(table):
            op.execute(stmt)

    op.drop_index("ix_lead_drip_state_due", table_name="lead_drip_state")
    op.drop_index(op.f("ix_lead_drip_state_tenant_id"), table_name="lead_drip_state")
    op.drop_table("lead_drip_state")

    op.drop_table("assignment_rules")

    op.drop_index("ix_lead_activities_lead_created", table_name="lead_activities")
    op.drop_index(op.f("ix_lead_activities_tenant_id"), table_name="lead_activities")
    op.drop_table("lead_activities")

    op.drop_index("ix_leads_tenant_agent_stage", table_name="leads")
    op.drop_index("ix_leads_tenant_stage_created", table_name="leads")
    op.drop_index(op.f("ix_leads_listing_id"), table_name="leads")
    op.drop_index(op.f("ix_leads_contact_id"), table_name="leads")
    op.drop_index(op.f("ix_leads_tenant_id"), table_name="leads")
    op.drop_table("leads")

    op.drop_index("ix_contacts_tenant_phone", table_name="contacts")
    op.drop_index("ix_contacts_tenant_email", table_name="contacts")
    op.drop_index(op.f("ix_contacts_tenant_id"), table_name="contacts")
    op.drop_table("contacts")
