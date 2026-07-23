"""compliance: consent records, cookie config, DSR requests (tenant RLS)

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-23

Part 23 (§8.17). Three tenant-owned tables, all fail-closed RLS:

- ``consent_records`` — append-only consent proof (what/when/evidence + a
  reference to the versioned legal page consented to). A subject is a user id,
  an email, or a session id (CHECK: at least one). ``user_id`` / ``legal_page_id``
  are SET NULL on delete — the *proof* must outlive the account/policy it names.
- ``cookie_consent_configs`` — one per tenant: category set + copy + defaults.
- ``dsr_requests`` — data-subject request lifecycle (export/erasure). ``user_id``
  SET NULL so the record survives the account it erased.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSENT_CATEGORY = sa.Enum(
    "necessary", "analytics", "marketing",
    name="consent_category", native_enum=False, length=20,
)
DSR_KIND = sa.Enum("export", "erasure", name="dsr_kind", native_enum=False, length=20)
DSR_STATUS = sa.Enum(
    "pending", "completed", "cancelled",
    name="dsr_status", native_enum=False, length=20,
)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    ]


def upgrade() -> None:
    # ---- consent_records ----
    op.create_table(
        "consent_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("category", CONSENT_CATEGORY, nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False),
        sa.Column("legal_page_id", sa.Uuid(), nullable=True),
        sa.Column("legal_version", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=60), nullable=False),
        sa.Column("ip", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=400), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "user_id IS NOT NULL OR email IS NOT NULL OR session_id IS NOT NULL",
            name="ck_consent_records_subject_present",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name=op.f("fk_consent_records_tenant_id_tenants"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_consent_records_user_id_users"), ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["legal_page_id"], ["legal_pages.id"],
            name=op.f("fk_consent_records_legal_page_id_legal_pages"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consent_records")),
    )
    op.create_index(
        op.f("ix_consent_records_tenant_id"), "consent_records", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_consent_records_email"), "consent_records", ["email"], unique=False
    )
    op.create_index(
        op.f("ix_consent_records_session_id"), "consent_records", ["session_id"], unique=False
    )
    op.create_index(
        "ix_consent_records_tenant_user", "consent_records", ["tenant_id", "user_id"], unique=False
    )
    op.create_index(
        "ix_consent_records_tenant_email", "consent_records", ["tenant_id", "email"], unique=False
    )
    for stmt in enable_tenant_rls_sql("consent_records"):
        op.execute(stmt)

    # ---- cookie_consent_configs ----
    op.create_table(
        "cookie_consent_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "categories", postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"), nullable=False,
        ),
        sa.Column(
            "banner_copy", postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column(
            "is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name=op.f("fk_cookie_consent_configs_tenant_id_tenants"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cookie_consent_configs")),
        sa.UniqueConstraint("tenant_id", name="uq_cookie_consent_configs_tenant"),
    )
    op.create_index(
        op.f("ix_cookie_consent_configs_tenant_id"),
        "cookie_consent_configs", ["tenant_id"], unique=False,
    )
    for stmt in enable_tenant_rls_sql("cookie_consent_configs"):
        op.execute(stmt)

    # ---- dsr_requests ----
    op.create_table(
        "dsr_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("subject_email", sa.String(length=320), nullable=True),
        sa.Column("kind", DSR_KIND, nullable=False),
        sa.Column("status", DSR_STATUS, server_default="pending", nullable=False),
        sa.Column("purge_scheduled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "result", postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column("ip", sa.String(length=45), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name=op.f("fk_dsr_requests_tenant_id_tenants"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_dsr_requests_user_id_users"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dsr_requests")),
    )
    op.create_index(
        op.f("ix_dsr_requests_tenant_id"), "dsr_requests", ["tenant_id"], unique=False
    )
    for stmt in enable_tenant_rls_sql("dsr_requests"):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in disable_tenant_rls_sql("dsr_requests"):
        op.execute(stmt)
    op.drop_index(op.f("ix_dsr_requests_tenant_id"), table_name="dsr_requests")
    op.drop_table("dsr_requests")

    for stmt in disable_tenant_rls_sql("cookie_consent_configs"):
        op.execute(stmt)
    op.drop_index(
        op.f("ix_cookie_consent_configs_tenant_id"), table_name="cookie_consent_configs"
    )
    op.drop_table("cookie_consent_configs")

    for stmt in disable_tenant_rls_sql("consent_records"):
        op.execute(stmt)
    op.drop_index("ix_consent_records_tenant_email", table_name="consent_records")
    op.drop_index("ix_consent_records_tenant_user", table_name="consent_records")
    op.drop_index(op.f("ix_consent_records_session_id"), table_name="consent_records")
    op.drop_index(op.f("ix_consent_records_email"), table_name="consent_records")
    op.drop_index(op.f("ix_consent_records_tenant_id"), table_name="consent_records")
    op.drop_table("consent_records")
