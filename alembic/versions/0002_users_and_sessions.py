"""users and sessions (identity tables, nullable-tenant RLS)

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-15

Both tables carry a nullable ``tenant_id`` — platform-staff rows have none
(§7.2) — and use the identity-RLS policy (``enable_identity_rls_sql``):
tenant-scoped sessions see exactly their tenant's rows, unscoped (platform)
sessions see exactly the NULL-tenant rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.rls import disable_identity_rls_sql, enable_identity_rls_sql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "buyer_renter",
                "seller",
                "agent",
                "team_lead",
                "admin",
                "marketing",
                "platform_admin",
                "platform_support",
                name="user_role",
                native_enum=False,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("active", "disabled", name="user_status", native_enum=False, length=20),
            server_default="active",
            nullable=False,
        ),
        sa.Column("first_name", sa.String(length=80), nullable=True),
        sa.Column("last_name", sa.String(length=80), nullable=True),
        sa.Column("locale", sa.String(length=10), server_default="fr", nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("mfa_secret", sa.String(length=64), nullable=True),
        sa.Column("email_verified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
            name=op.f("fk_users_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint(
            "tenant_id",
            "email",
            name=op.f("uq_users_tenant_id"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(op.f("ix_users_tenant_id"), "users", ["tenant_id"], unique=False)

    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("refresh_token_hash", sa.String(length=64), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("user_agent", sa.String(length=400), nullable=True),
        sa.Column("ip", sa.String(length=45), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
            ["user_id"], ["users.id"], name=op.f("fk_sessions_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("refresh_token_hash", name=op.f("uq_sessions_refresh_token_hash")),
    )
    op.create_index(op.f("ix_sessions_user_id"), "sessions", ["user_id"], unique=False)
    op.create_index(op.f("ix_sessions_tenant_id"), "sessions", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_sessions_family_id"), "sessions", ["family_id"], unique=False)

    for table in ("users", "sessions"):
        for stmt in enable_identity_rls_sql(table):
            op.execute(stmt)


def downgrade() -> None:
    for table in ("sessions", "users"):
        for stmt in disable_identity_rls_sql(table):
            op.execute(stmt)
    op.drop_index(op.f("ix_sessions_family_id"), table_name="sessions")
    op.drop_index(op.f("ix_sessions_tenant_id"), table_name="sessions")
    op.drop_index(op.f("ix_sessions_user_id"), table_name="sessions")
    op.drop_table("sessions")
    op.drop_index(op.f("ix_users_tenant_id"), table_name="users")
    op.drop_table("users")
