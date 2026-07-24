"""auth hardening: MFA columns, session labels, OAuth identity links

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-24

Part 29 (§7.1, §10.3).

- ``users.mfa_secret`` is widened from 64 to 255: the column was reserved in
  Part 3 for a *plaintext* base32 seed (32 chars), but the secret is now
  field-encrypted at rest (§10.7, Part 30's ``EncryptedString``) and the
  ``{key_id}:{b64(nonce+ct)}`` envelope runs ~83 chars — with headroom for a
  longer key id after a rotation.
- ``users.mfa_enabled`` / ``mfa_enrolled_at`` split "a secret exists" (mid
  enrolment, not yet proven) from "the second factor is live". Only an enabled
  factor is ever demanded at login.
- ``users.mfa_pending_secret`` holds an in-progress (re-)enrolment's seed,
  promoted to ``mfa_secret`` only when a code proves the person holds it. This
  keeps a re-enrolment from ever overwriting the *live* secret before the new
  factor is confirmed — abandoning a re-enrolment must not break login.
- ``sessions.last_used_at`` powers the session-list endpoint (§10.3): "log out
  other devices" is only usable if a person can tell which row is which.
- ``oauth_identities`` links an external provider subject to a local account.
  The table ships now so the OAuth seam has somewhere to land the day real
  credentials exist; it is global (like ``users``/``sessions``) with the same
  identity-RLS policy, since a platform-staff account may link one too.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.rls import disable_identity_rls_sql, enable_identity_rls_sql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "users",
        "mfa_secret",
        existing_type=sa.String(length=64),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
    op.add_column("users", sa.Column("mfa_pending_secret", sa.String(length=255), nullable=True))
    op.add_column(
        "users",
        sa.Column("mfa_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("users", sa.Column("mfa_enrolled_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("sessions", sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True))

    op.create_table(
        "oauth_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
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
            ["user_id"],
            ["users.id"],
            name=op.f("fk_oauth_identities_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_identities")),
        # One local account per (tenant, provider, external subject): the same
        # Google account may legitimately hold an account at two agencies.
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "subject",
            name="uq_oauth_identities_tenant_provider_subject",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        op.f("ix_oauth_identities_user_id"), "oauth_identities", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_oauth_identities_tenant_id"), "oauth_identities", ["tenant_id"], unique=False
    )
    for stmt in enable_identity_rls_sql("oauth_identities"):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in disable_identity_rls_sql("oauth_identities"):
        op.execute(stmt)
    op.drop_index(op.f("ix_oauth_identities_tenant_id"), table_name="oauth_identities")
    op.drop_index(op.f("ix_oauth_identities_user_id"), table_name="oauth_identities")
    op.drop_table("oauth_identities")

    op.drop_column("sessions", "last_used_at")
    op.drop_column("users", "mfa_enrolled_at")
    op.drop_column("users", "mfa_enabled")
    op.drop_column("users", "mfa_pending_secret")
    op.alter_column(
        "users",
        "mfa_secret",
        existing_type=sa.String(length=255),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
