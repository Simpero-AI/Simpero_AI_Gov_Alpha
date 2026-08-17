import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entity_resolution import EntityResolution
from app.repo.BaseRepo import BaseRepo
from app.services.entity_resolution.types import Resolution


class EntityResolutionRepo(BaseRepo[EntityResolution, dict]):
    """Write-once rows -- there is deliberately no update path here, and the
    database refuses UPDATE/DELETE from dd_app regardless (see this table's
    migration). Re-resolving a deal appends a new row."""

    def __init__(self, db: AsyncSession):
        super().__init__(db)

    async def create(self, data: dict, **kwargs: object) -> EntityResolution:
        row = EntityResolution(**data)
        self.session.add(row)
        return row

    async def get_by_id(self, id: object) -> EntityResolution | None:
        return await self.session.get(EntityResolution, id)

    async def record(
        self,
        resolution: Resolution,
        *,
        org_id: int,
        deal_id: uuid.UUID,
    ) -> EntityResolution:
        """Persist one resolution attempt.

        `source` and `query_name` come off the resolution itself, never
        caller-supplied strings -- the point of storing them is that they name
        the registry that ACTUALLY answered and the name that was ACTUALLY
        searched, which is what makes an old row re-readable after the deal is
        renamed or a second registry is added.
        """
        return await self.create(
            {
                "org_id": org_id,
                "deal_id": deal_id,
                "source": resolution.source,
                "status": resolution.status,
                "query_name": resolution.query_name,
                "registry_id": resolution.registry_id,
                "legal_name": resolution.legal_name,
                "former_names": [f.to_json() for f in resolution.former_names],
                "matched_on": resolution.matched_on,
                "reason": resolution.reason,
                "evidence": resolution.evidence,
            }
        )

    async def latest_for_deal(self, deal_id: uuid.UUID) -> EntityResolution | None:
        """Most recent resolution of a deal. Rows are append-only, so "latest"
        is the current anchor and the older rows are the history of how it
        changed -- e.g. a deal that was `not_found` before the company filed,
        then `resolved` after.

        created_at is a clock_timestamp() (see the model), so it genuinely
        advances between two writes even inside one transaction. `id` is the
        secondary sort only so the query is TOTALLY ordered -- it is a random
        UUID, so a stability tiebreak, not a recency signal.
        """
        result = await self.session.execute(
            select(EntityResolution)
            .where(EntityResolution.deal_id == deal_id)
            .order_by(EntityResolution.created_at.desc(), EntityResolution.id.desc())
            .limit(1)
        )
        return result.scalars().first()
