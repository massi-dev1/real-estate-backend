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
