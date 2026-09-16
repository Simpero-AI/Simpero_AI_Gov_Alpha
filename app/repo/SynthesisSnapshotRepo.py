import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.synthesis_snapshot import SynthesisSnapshot
from app.repo.BaseRepo import BaseRepo


class SynthesisSnapshotRepo(BaseRepo[SynthesisSnapshot, dict]):
    """Write-once rows -- there is deliberately no update path here, and the
    database refuses UPDATE/DELETE from dd_app regardless (see this table's
    migration). A re-analysis appends a new row; the reader takes the latest."""

    def __init__(self, db: AsyncSession):
        super().__init__(db)

    async def create(self, data: dict, **kwargs: object) -> SynthesisSnapshot:
        row = SynthesisSnapshot(**data)
        self.session.add(row)
        return row

    async def get_by_id(self, id: object) -> SynthesisSnapshot | None:
        return await self.session.get(SynthesisSnapshot, id)

    async def record(
        self,
        *,
        org_id: int,
        deal_id: uuid.UUID,
        analysis_run_id: uuid.UUID | None,
        sections: list,
        reason: str,
    ) -> SynthesisSnapshot:
        """Persist one synthesis pass. `sections` is the already-serialized JSONB
        shape ([{key,title,points:[...]}], empty when nothing grounded); `reason`
        is the deal-level sentinel (see SynthesisSnapshot.REASONS). org_id is
        stamped explicitly so the FORCE-RLS INSERT is accepted -- same idiom as
        persist_web_facts."""
        return await self.create(
            {
                "org_id": org_id,
                "deal_id": deal_id,
                "analysis_run_id": analysis_run_id,
                "sections": sections,
                "reason": reason,
            }
        )

    async def latest_for_deal(self, deal_id: uuid.UUID) -> SynthesisSnapshot | None:
        """Most recent synthesis snapshot of a deal. Rows are append-only, so
        "latest" is the current answer and older rows are the history of how the
        summaries changed across re-analyses.

        created_at is a clock_timestamp() (see the model), so it advances between
        two writes even inside one transaction. `id` is the secondary sort only
        so the query is TOTALLY ordered -- a stability tiebreak, not a recency
        signal (it is a random UUID)."""
        result = await self.session.execute(
            select(SynthesisSnapshot)
            .where(SynthesisSnapshot.deal_id == deal_id)
            .order_by(SynthesisSnapshot.created_at.desc(), SynthesisSnapshot.id.desc())
            .limit(1)
        )
        return result.scalars().first()
