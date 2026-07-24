"""agent profiles, teams, team members (tenant RLS)

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-17

All three tables are strictly tenant-owned and get the fail-closed tenant RLS
policy. ``agent_profiles.service_areas`` (MultiPolygon, GiST) is the data
source for territory-based lead assignment (§8.4/§8.5); ``team_members`` is
what gives ``team_lead`` real team-scoped visibility.
"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AGENT_PHOTO_STATUS = sa.Enum(
    "pending",
    "processing",
    "ready",
    "failed",
    name="agent_photo_status",
    native_enum=False,
    length=20,
)


def upgrade() -> None:
    op.create_table(
        "agent_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("bio", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column(
            "specialties",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "service_areas",
            geoalchemy2.types.Geometry(
                geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False
            ),
            nullable=True,
        ),
        sa.Column("license_no", sa.String(length=100), nullable=True),
        sa.Column(
            "socials", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("is_published", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("photo_key", sa.String(length=300), nullable=True),
        sa.Column("photo_status", AGENT_PHOTO_STATUS, nullable=True),
        sa.Column(
            "photo_variants",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("photo_error", sa.String(length=200), nullable=True),
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
            name=op.f("fk_agent_profiles_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_agent_profiles_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_profiles")),
        sa.UniqueConstraint(
            "tenant_id", "user_id", name=op.f("uq_agent_profiles_tenant_id_user_id")
        ),
        sa.UniqueConstraint("tenant_id", "slug", name=op.f("uq_agent_profiles_tenant_id_slug")),
    )
    op.create_index(
        op.f("ix_agent_profiles_tenant_id"), "agent_profiles", ["tenant_id"], unique=False
    )
    # Territory lookup: ST_Contains(service_areas, point) over published rows.
    op.create_index(
        "ix_agent_profiles_service_areas",
        "agent_profiles",
        ["service_areas"],
        unique=False,
        postgresql_using="gist",
    )

    op.create_table(
        "teams",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("lead_user_id", sa.Uuid(), nullable=True),
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
            name=op.f("fk_teams_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lead_user_id"],
            ["users.id"],
            name=op.f("fk_teams_lead_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_teams")),
    )
    op.create_index(op.f("ix_teams_tenant_id"), "teams", ["tenant_id"], unique=False)

    op.create_table(
        "team_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role_in_team", sa.String(length=40), nullable=True),
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
            name=op.f("fk_team_members_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name=op.f("fk_team_members_team_id_teams"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_team_members_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_team_members")),
        sa.UniqueConstraint("team_id", "user_id", name=op.f("uq_team_members_team_id_user_id")),
    )
    op.create_index(op.f("ix_team_members_tenant_id"), "team_members", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_team_members_user_id"), "team_members", ["user_id"], unique=False)

    for table in ("agent_profiles", "teams", "team_members"):
        for stmt in enable_tenant_rls_sql(table):
            op.execute(stmt)


def downgrade() -> None:
    for table in ("team_members", "teams", "agent_profiles"):
        for stmt in disable_tenant_rls_sql(table):
            op.execute(stmt)

    op.drop_index(op.f("ix_team_members_user_id"), table_name="team_members")
    op.drop_index(op.f("ix_team_members_tenant_id"), table_name="team_members")
    op.drop_table("team_members")

    op.drop_index(op.f("ix_teams_tenant_id"), table_name="teams")
    op.drop_table("teams")

    op.drop_index("ix_agent_profiles_service_areas", table_name="agent_profiles")
    op.drop_index(op.f("ix_agent_profiles_tenant_id"), table_name="agent_profiles")
    op.drop_table("agent_profiles")
