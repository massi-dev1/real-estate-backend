"""Row-level-security DDL helpers used by Alembic migrations (§4.2).

Every tenant-owned table gets the same pair of statements. The policy reads
``current_setting('app.tenant_id')`` *without* ``missing_ok``, so a session
that forgot ``SET LOCAL app.tenant_id`` errors instead of silently seeing
nothing — fail closed, loudly.
"""


def enable_tenant_rls_sql(table: str) -> tuple[str, ...]:
    """DDL statements enabling tenant isolation on ``table`` (needs a ``tenant_id`` column)."""
    return (
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        (
            f"CREATE POLICY tenant_isolation ON {table} "
            "USING (tenant_id = current_setting('app.tenant_id')::uuid)"
        ),
    )


def disable_tenant_rls_sql(table: str) -> tuple[str, ...]:
    return (
        f"DROP POLICY tenant_isolation ON {table}",
        f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY",
    )


def enable_identity_rls_sql(table: str) -> tuple[str, ...]:
    """RLS for identity tables (``users``, ``sessions``) whose ``tenant_id`` is
    nullable because platform-staff rows have no tenant (§7.2).

    Deliberate deviation from :func:`enable_tenant_rls_sql`: the policy reads
    the GUC with ``missing_ok`` so that platform requests (which never set
    ``app.tenant_id``) can reach the NULL-tenant rows. ``IS NOT DISTINCT FROM``
    keeps it strict in both directions — a tenant-scoped session sees exactly
    its tenant's rows, an unscoped session sees exactly the platform rows, and
    neither can ever read (or write, via the implicit WITH CHECK) the other's.
    """
    return (
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        (
            f"CREATE POLICY tenant_isolation ON {table} USING (tenant_id IS NOT DISTINCT FROM "
            "NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
        ),
    )


def disable_identity_rls_sql(table: str) -> tuple[str, ...]:
    return disable_tenant_rls_sql(table)
