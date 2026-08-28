import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.resolved_entity import ResolvedEntity
from app.repo.BaseRepo import BaseRepo


class ResolvedEntityRepo(BaseRepo[ResolvedEntity, dict]):
    """Write-once rows -- there is deliberately no update path here, and the
    database refuses UPDATE/DELETE from dd_app regardless (see this table's
    migration). Re-folding a deal appends a new row."""

    def __init__(self, db: AsyncSession):
        super().__init__(db)

    async def create(self, data: dict, **kwargs: object) -> ResolvedEntity:
        row = ResolvedEntity(**data)
        self.session.add(row)
        return row

    async def get_by_id(self, id: object) -> ResolvedEntity | None:
        return await self.session.get(ResolvedEntity, id)

    async def latest_for_deal(self, deal_id: uuid.UUID) -> ResolvedEntity | None:
        """The deal's current identity. Rows are append-only, so "latest" is the
        anchor in force and the older rows are the history of how it changed --
        e.g. a deal folded from EDGAR alone, then re-folded once ISED answered
        too.

        created_at is a clock_timestamp() (see the model), so it genuinely
        advances between two writes even inside one transaction. `id` is the
        secondary sort only so the query is TOTALLY ordered -- it is a random
        UUID, so a stability tiebreak, not a recency signal. Same idiom as
        EntityResolutionRepo.latest_for_deal.
        """
        result = await self.session.execute(
            select(ResolvedEntity)
            .where(ResolvedEntity.deal_id == deal_id)
            .order_by(ResolvedEntity.created_at.desc(), ResolvedEntity.id.desc())
            .limit(1)
        )
        return result.scalars().first()
