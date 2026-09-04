from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.claim import Claim
from app.models.corroboration_event import CorroborationEvent
from app.repo.BaseRepo import BaseRepo


class CorroborationEventRepo(BaseRepo[CorroborationEvent, dict]):
    """Sole write path to corroboration_events. INSERT-only, deliberately:
    there is no update/delete method here, and the database refuses both
    regardless (REVOKE UPDATE, DELETE ON corroboration_events FROM dd_app —
    see the migration that creates the table). Do not add one.
    """

    def __init__(self, db: AsyncSession):
        super().__init__(db)

    async def append(self, data: dict, **kwargs: object) -> CorroborationEvent:
        """Insert one corroboration event. Named `append`, not `create`, to
        make the insert-only contract explicit at call sites (mirrors
        HumanAuditRepo)."""
        row = CorroborationEvent(**data)
        self.session.add(row)
        return row

    async def create(self, data: dict, **kwargs: object) -> CorroborationEvent:
        return await self.append(data, **kwargs)

    async def get_by_id(self, id: object) -> CorroborationEvent | None:
        return await self.session.get(CorroborationEvent, id)

    async def list_for_claim(self, claim_id: object) -> list[CorroborationEvent]:
        """Every outside-source check ever run against one claim, oldest
        first — the external half of "surface the disagreement itself" (the
        claim's own `value` is the document-sourced half; this is the rest).
        """
        result = await self.session.execute(
            select(CorroborationEvent)
            .where(CorroborationEvent.claim_id == claim_id)
            .order_by(CorroborationEvent.created_at.asc())
        )
        return list(result.scalars().all())

    async def list_for_deal(self, deal_id: object) -> list[tuple[CorroborationEvent, Claim]]:
        """Every outside-source check run against any of a deal's claims, each
        paired with the claim it targeted, newest first. corroboration_events
        has no deal_id column by design (it links to a claim, not a deal), so
        this joins through claims -- the deal-scoped read the
        /deals/{id}/corroboration endpoint serves. RLS on both tables keeps it
        org-scoped; newest-first lets the caller keep the latest check per
        (claim, source) across re-analysis generations."""
        result = await self.session.execute(
            select(CorroborationEvent, Claim)
            .join(Claim, CorroborationEvent.claim_id == Claim.id)
            .where(Claim.deal_id == deal_id)
            .order_by(CorroborationEvent.created_at.desc())
        )
        return [(event, claim) for event, claim in result.all()]
