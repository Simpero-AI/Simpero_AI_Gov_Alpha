from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_profile import InvestmentProfile
from app.repo.BaseRepo import BaseRepo


class InvestmentProfileRepo(BaseRepo[InvestmentProfile, dict]):
    def __init__(self, db: AsyncSession):
        super().__init__(db)

    async def create(self, data: dict, **kwargs: object) -> InvestmentProfile:
        # Plain insert only — upsert_for_org below is the create-or-update path
        # investmentProfile.upsert uses; this stays for a caller that has a
        # brand-new row and no conflict to resolve.
        profile = InvestmentProfile(**data)
        self.session.add(profile)
        return profile

    async def upsert_for_org(self, org_id: int, data: dict) -> InvestmentProfile:
        """Create-or-update the org's single profile row (org_id is UNIQUE) —
        Phase 2's create-or-update that create() above deferred. `data` carries
        only the caller-provided columns (model_dump(exclude_unset=True)), so a
        save of one slice (firm_name+mandate, or weights) leaves the other
        columns as they were; each JSONB column is a full replace of what the
        caller sent. updated_at is bumped explicitly because pg_insert bypasses
        the ORM onupdate=now() event. RLS's USING clause doubles as the
        INSERT/UPDATE WITH CHECK, so org_id must be the caller's own org."""
        result = await self.session.execute(
            pg_insert(InvestmentProfile)
            .values(org_id=org_id, **data)
            .on_conflict_do_update(
                index_elements=["org_id"],
                set_={**data, "updated_at": func.now()},
            )
            .returning(InvestmentProfile),
            # populate_existing so a RETURNING row always wins over a stale
            # identity-map instance, same guard as MandateRepo.upsert.
            execution_options={"populate_existing": True},
        )
        return result.scalar_one()

    async def get_by_id(self, id: object) -> InvestmentProfile | None:
        return await self.session.get(InvestmentProfile, id)

    async def get_for_org(self) -> InvestmentProfile | None:
        """One row per org (org_id is UNIQUE) — RLS scopes this to the
        request's org, no WHERE org_id needed."""
        result = await self.session.execute(select(InvestmentProfile).limit(1))
        return result.scalar_one_or_none()
