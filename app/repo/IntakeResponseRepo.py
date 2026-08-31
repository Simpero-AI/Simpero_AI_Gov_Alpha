import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deal_intake_response import DealIntakeResponse
from app.repo.BaseRepo import BaseRepo


class IntakeResponseRepo(BaseRepo[DealIntakeResponse, dict]):
    """Reads of the external party's submitted answers (P3-05). There is no
    update or delete method here and there should never be one: the table is
    blanket-immutable at the database (REVOKE UPDATE, DELETE ON
    deal_intake_response FROM dd_app -- the human_audit_log idiom), because a
    submitted answer is a historical fact, not editable state.
    """

    def __init__(self, db: AsyncSession):
        super().__init__(db)

    async def create(self, data: dict, **kwargs: object) -> DealIntakeResponse:
        response = DealIntakeResponse(**data)
        self.session.add(response)
        return response

    async def get_by_id(self, id: object) -> DealIntakeResponse | None:
        return await self.session.get(DealIntakeResponse, id)

    async def latest_for_deal(self, deal_id: uuid.UUID) -> DealIntakeResponse | None:
        """The deal's most recent submission. Deliberately `latest`, not a
        single-row lookup: Q5 settled that a reissued link's submission is a
        NEW row rather than an edit of the old one, so a deal that was
        collected from twice has two rows here and the org-side reader wants
        the newer one.

        Ordered by `created_at` (NOT NULL, `now()` default) rather than
        `submitted_at` (nullable), with `id` as the tie-breaker because
        `now()` is transaction time -- two rows written in one transaction
        share a timestamp exactly and the ordering would otherwise be
        arbitrary between them.
        """
        result = await self.session.execute(
            select(DealIntakeResponse)
            .where(DealIntakeResponse.deal_id == deal_id)
            .order_by(DealIntakeResponse.created_at.desc(), DealIntakeResponse.id.desc())
            .limit(1)
        )
        return result.scalars().first()
