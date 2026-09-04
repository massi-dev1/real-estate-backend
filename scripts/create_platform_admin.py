"""Bootstrap the first platform admin (there is no signup for platform staff).

Usage:
    uv run python scripts/create_platform_admin.py admin@example.com
    (password read from PLATFORM_ADMIN_PASSWORD or prompted)

Further staff accounts are created through POST /api/v1/platform/staff by an
authenticated platform admin.
"""

import argparse
import asyncio
import getpass
import os
import sys

from app.core.config import get_settings
from app.core.database import create_engine, create_session_factory
from app.core.permissions import Role

# `users.tenant_id` carries an FK to `tenants`, which SQLAlchemy resolves lazily
# on first use. Importing the users module alone leaves `tenants` absent from
# Base.metadata and the mapper raises NoReferencedTableError, so pull in the
# model registry (which imports every module's models) before the repository.
from app.modules.tenants import models as _tenant_models  # noqa: F401
from app.modules.users.repository import UserRepository
from app.modules.users.service import UserService


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email")
    args = parser.parse_args()

    password = os.environ.get("PLATFORM_ADMIN_PASSWORD") or getpass.getpass("Password: ")
    if len(password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        return 1

    engine = create_engine(get_settings())
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session, session.begin():
            service = UserService(UserRepository(session))
            user = await service.create_account(
                None,
                email=args.email.strip().lower(),
                password=password,
                role=Role.PLATFORM_ADMIN,
            )
            print(f"Platform admin created: {user.email} ({user.id})")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
