from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_profile import InvestmentProfile
from app.repo.BaseRepo import BaseRepo


class InvestmentProfileRepo(BaseRepo[InvestmentProfile, dict]):
    def __init__(self, db: AsyncSession):
        super().__init__(db)

    async def create(self, data: dict, **kwargs: object) -> InvestmentProfile:
        # Plain insert only — the upsert (create-or-update on org_id) that
        # investmentProfile.upsert needs arrives in Phase 2.
        profile = InvestmentProfile(**data)
        self.session.add(profile)
        return profile

    async def get_by_id(self, id: object) -> InvestmentProfile | None:
        return await self.session.get(InvestmentProfile, id)

    async def get_for_org(self) -> InvestmentProfile | None:
        """One row per org (org_id is UNIQUE) — RLS scopes this to the
        request's org, no WHERE org_id needed."""
        result = await self.session.execute(select(InvestmentProfile).limit(1))
        return result.scalar_one_or_none()

    async def upsert(self, data: dict) -> InvestmentProfile:
        """Create-or-update the org's single investment-profile row, keyed on the
        unique org_id. `data` is the FULL row to persist (firm_name, firm_type,
        aum_band, mandate, weights, org_id) -- the endpoint merges any partial
        request over the stored row before calling this, so a save that touches
        only the framework weights doesn't blank the firm profile. Mirrors
        MandateRepo.upsert: populate_existing so the RETURNING row wins over any
        instance the endpoint already loaded (get_for_org, for the merge), rather
        than echoing the pre-update values back to the caller."""
        result = await self.session.execute(
            pg_insert(InvestmentProfile)
            .values(**data)
            .on_conflict_do_update(
                index_elements=["org_id"],
                set_={
                    "firm_name": data.get("firm_name"),
                    "firm_type": data.get("firm_type"),
                    "aum_band": data.get("aum_band"),
                    "mandate": data.get("mandate"),
                    "weights": data.get("weights"),
                },
            )
            .returning(InvestmentProfile),
            execution_options={"populate_existing": True},
        )
        return result.scalar_one()
