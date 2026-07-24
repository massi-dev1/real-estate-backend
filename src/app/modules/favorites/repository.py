"""DB access for favorites & saved searches. Every method takes ``tenant_id``
(golden rule §5); favorites and account-owned saved searches are additionally
scoped to their owning ``user_id`` — self-owned rows, no roles involved.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_utils.compat import uuid7

from app.modules.favorites.models import AlertFrequency, Favorite, SavedSearch


class FavoritesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---- favorites ----

    async def upsert_favorite(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID, listing_id: uuid.UUID
    ) -> None:
        """Idempotent PUT: ON CONFLICT DO NOTHING — a double-tap on the heart
        icon must not error or duplicate."""
        stmt = (
            pg_insert(Favorite)
            .values(id=uuid7(), tenant_id=tenant_id, user_id=user_id, listing_id=listing_id)
            .on_conflict_do_nothing(index_elements=["tenant_id", "user_id", "listing_id"])
        )
        await self.session.execute(stmt)

    async def delete_favorite(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID, listing_id: uuid.UUID
    ) -> None:
        await self.session.execute(
            delete(Favorite).where(
                Favorite.tenant_id == tenant_id,
                Favorite.user_id == user_id,
                Favorite.listing_id == listing_id,
            )
        )

    async def list_favorites(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[Favorite]:
        """Keyset page on (created_at DESC, id DESC); returns limit+1 rows."""
        stmt = select(Favorite).where(Favorite.tenant_id == tenant_id, Favorite.user_id == user_id)
        if after is not None:
            stmt = stmt.where(
                or_(
                    Favorite.created_at < after[0],
                    and_(Favorite.created_at == after[0], Favorite.id < after[1]),
                )
            )
        stmt = stmt.order_by(Favorite.created_at.desc(), Favorite.id.desc()).limit(limit + 1)
        return list((await self.session.execute(stmt)).scalars())

    # ---- saved searches ----

    async def get_saved_search(
        self,
        tenant_id: uuid.UUID,
        saved_search_id: uuid.UUID,
        *,
        user_id: uuid.UUID | None = None,
    ) -> SavedSearch | None:
        """``user_id`` narrows to that account's rows (the /me surface); the
        token flows (confirm/unsubscribe) resolve by tenant + id alone — the
        single-use or signed token is the authorization there."""
        stmt = select(SavedSearch).where(
            SavedSearch.tenant_id == tenant_id, SavedSearch.id == saved_search_id
        )
        if user_id is not None:
            stmt = stmt.where(SavedSearch.user_id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_saved_searches(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[SavedSearch]:
        stmt = (
            select(SavedSearch)
            .where(SavedSearch.tenant_id == tenant_id, SavedSearch.user_id == user_id)
            .order_by(SavedSearch.created_at.desc(), SavedSearch.id.desc())
        )
        return list((await self.session.execute(stmt)).scalars())

    async def count_saved_searches(self, tenant_id: uuid.UUID, user_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(SavedSearch)
            .where(SavedSearch.tenant_id == tenant_id, SavedSearch.user_id == user_id)
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def delete_saved_search(self, tenant_id: uuid.UUID, saved_search_id: uuid.UUID) -> None:
        await self.session.execute(
            delete(SavedSearch).where(
                SavedSearch.tenant_id == tenant_id, SavedSearch.id == saved_search_id
            )
        )

    async def list_active_by_frequency(
        self, tenant_id: uuid.UUID, frequencies: Sequence[AlertFrequency]
    ) -> list[SavedSearch]:
        """The alert matchers' scan (served by the composite index)."""
        stmt = select(SavedSearch).where(
            SavedSearch.tenant_id == tenant_id,
            SavedSearch.is_active.is_(True),
            SavedSearch.frequency.in_(list(frequencies)),
        )
        return list((await self.session.execute(stmt)).scalars())

    # ---- compliance boundary (§8.17): DSR export + erasure ----

    async def all_favorites_for_user(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[Favorite]:
        stmt = select(Favorite).where(Favorite.tenant_id == tenant_id, Favorite.user_id == user_id)
        return list((await self.session.execute(stmt)).scalars())

    async def delete_all_for_user(self, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """Hard-delete a user's favorites + saved searches (erasure §10.12) —
        these are personal preference rows with no business-record value to
        retain, unlike a CRM lead."""
        await self.session.execute(
            delete(Favorite).where(Favorite.tenant_id == tenant_id, Favorite.user_id == user_id)
        )
        await self.session.execute(
            delete(SavedSearch).where(
                SavedSearch.tenant_id == tenant_id, SavedSearch.user_id == user_id
            )
        )

    def add(self, obj: Favorite | SavedSearch) -> None:
        self.session.add(obj)

    async def flush(self) -> None:
        await self.session.flush()
