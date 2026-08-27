from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mandate import Mandate
from app.repo.BaseRepo import BaseRepo


class MandateRepo(BaseRepo[Mandate, dict]):
    def __init__(self, db: AsyncSession):
        super().__init__(db)

    async def create(self, data: dict, **kwargs: object) -> Mandate:
        mandate = Mandate(**data)
        self.session.add(mandate)
        return mandate

    async def get_by_id(self, id: object) -> Mandate | None:
        return await self.session.get(Mandate, id)

    async def get_for_org(self) -> Mandate | None:
        """One row per org (org_id is UNIQUE) -- RLS scopes this to the
        request's org, no WHERE org_id needed."""
        result = await self.session.execute(select(Mandate).limit(1))
        return result.scalar_one_or_none()

    async def upsert(self, data: dict) -> Mandate:
        """Create-or-update on org_id -- the product portal's PUT /mandate
        is always "set the org's mandate", never a partial patch."""
        result = await self.session.execute(
            pg_insert(Mandate)
            .values(**data)
            .on_conflict_do_update(
                index_elements=["org_id"],
                set_={"user_id": data["user_id"], "mandate": data["mandate"]},
            )
            .returning(Mandate),
            # Without this the ORM hands back the instance already in the
            # identity map -- and PUT /mandate always loads the previous row
            # (for the audit diff) before calling this, so the RETURNING values
            # were being discarded and the response echoed the OLD mandate back
            # to the caller. The row on disk was always correct; only what the
            # frontend got to render was stale.
            execution_options={"populate_existing": True},
        )
        return result.scalar_one()
