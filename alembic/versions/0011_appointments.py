"""agent availability + appointments (§8.7, tenant RLS)

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-17

Two tenant-owned tables with the fail-closed tenant RLS policy:
``agent_availability`` (weekly template rows XOR dated exception rows) and
``appointments`` (the requested → confirmed → completed | cancelled | no_show
tour lifecycle, with reminder idempotency stamps for the Beat sweep).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPOINTMENT_STATUS = sa.Enum(
    "requested",
    "confirmed",
    "completed",
    "cancelled",
    "no_show",
    name="appointment_status",
    native_enum=False,
    length=20,
)


def upgrade() -> None:
    op.create_table(
        "agent_availability",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("agent_user_id", sa.Uuid(), nullable=False),
        sa.Column("day_of_week", sa.Integer(), nullable=True),
        sa.Column("date", sa.Date(), nullable=True),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("is_block", sa.Boolean(), server_default=sa.text("false"), nullable=False),
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
        sa.CheckConstraint(
            "(day_of_week IS NULL) != (date IS NULL)",
            name=op.f("ck_agent_availability_weekly_xor_exception"),
        ),
        sa.CheckConstraint(
            "start_time < end_time", name=op.f("ck_agent_availability_start_before_end")
        ),
        sa.CheckConstraint(
            "day_of_week IS NULL OR (day_of_week BETWEEN 0 AND 6)",
            name=op.f("ck_agent_availability_day_of_week_range"),
        ),
        sa.CheckConstraint(
            "NOT is_block OR date IS NOT NULL",
            name=op.f("ck_agent_availability_block_is_exception"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_agent_availability_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_user_id"],
            ["users.id"],
            name=op.f("fk_agent_availability_agent_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_availability")),
    )
    op.create_index(
        op.f("ix_agent_availability_tenant_id"), "agent_availability", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_agent_availability_agent_user_id"),
        "agent_availability",
        ["agent_user_id"],
        unique=False,
    )

    op.create_table(
        "appointments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("agent_user_id", sa.Uuid(), nullable=False),
        sa.Column("listing_id", sa.Uuid(), nullable=True),
        sa.Column("contact_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status", APPOINTMENT_STATUS, server_default=sa.text("'requested'"), nullable=False
        ),
        sa.Column("start_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("end_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("reminder_24h_sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("reminder_1h_sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.CheckConstraint("start_at < end_at", name=op.f("ck_appointments_start_before_end")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_appointments_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_user_id"],
            ["users.id"],
            name=op.f("fk_appointments_agent_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["listings.id"],
            name=op.f("fk_appointments_listing_id_listings"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name=op.f("fk_appointments_contact_id_contacts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"],
            ["leads.id"],
            name=op.f("fk_appointments_lead_id_leads"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_appointments")),
    )
    op.create_index(op.f("ix_appointments_tenant_id"), "appointments", ["tenant_id"], unique=False)
    # Slot conflict checks + the per-agent agenda and iCal feed.
    op.create_index(
        "ix_appointments_tenant_agent_start",
        "appointments",
        ["tenant_id", "agent_user_id", "start_at"],
        unique=False,
    )
    # The reminder sweep scans by start_at across a tenant.
    op.create_index(
        "ix_appointments_tenant_start", "appointments", ["tenant_id", "start_at"], unique=False
    )

    for table in ("agent_availability", "appointments"):
        for stmt in enable_tenant_rls_sql(table):
            op.execute(stmt)


def downgrade() -> None:
    for table in ("appointments", "agent_availability"):
        for stmt in disable_tenant_rls_sql(table):
            op.execute(stmt)

    op.drop_index("ix_appointments_tenant_start", table_name="appointments")
    op.drop_index("ix_appointments_tenant_agent_start", table_name="appointments")
    op.drop_index(op.f("ix_appointments_tenant_id"), table_name="appointments")
    op.drop_table("appointments")

    op.drop_index(op.f("ix_agent_availability_agent_user_id"), table_name="agent_availability")
    op.drop_index(op.f("ix_agent_availability_tenant_id"), table_name="agent_availability")
    op.drop_table("agent_availability")
