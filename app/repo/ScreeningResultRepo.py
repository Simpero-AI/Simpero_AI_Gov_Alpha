import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.screening_result import ScreeningResult
from app.repo.BaseRepo import BaseRepo
from app.services.screening.decision import ScreeningDecision


class ScreeningResultRepo(BaseRepo[ScreeningResult, dict]):
    """Write-once rows -- there is deliberately no update path here, and the
    database refuses UPDATE/DELETE from dd_app regardless (see this table's
    migration). Re-screening a deal appends a new row."""

    def __init__(self, db: AsyncSession):
        super().__init__(db)

    async def create(self, data: dict, **kwargs: object) -> ScreeningResult:
        row = ScreeningResult(**data)
        self.session.add(row)
        return row

    async def get_by_id(self, id: object) -> ScreeningResult | None:
        return await self.session.get(ScreeningResult, id)

    async def record(
        self,
        decision: ScreeningDecision,
        *,
        org_id: int,
        deal_id: uuid.UUID,
        analysis_run_id: uuid.UUID | None = None,
    ) -> ScreeningResult:
        """Persist one screening pass. The rulebook version comes off the
        decision itself, never a caller-supplied string -- the whole point of
        stamping it is that it names the rules that ACTUALLY ran."""
        return await self.create(
            {
                "org_id": org_id,
                "deal_id": deal_id,
                "analysis_run_id": analysis_run_id,
                "rulebook_version": decision.rulebook_version,
                "recommendation": decision.recommendation,
                "rule_results": [r.to_json() for r in decision.results],
            }
        )

    async def latest_for_deal(self, deal_id: uuid.UUID) -> ScreeningResult | None:
        """Most recent screening of a deal. Rows are append-only, so "latest"
        is the current answer and the older rows are the history of how the
        answer changed as the rulebook and the deal's claims evolved.

        created_at is a clock_timestamp() (see the model), so it genuinely
        advances between two writes even inside one transaction. `id` is the
        secondary sort only so the query is TOTALLY ordered -- it makes the
        result repeatable rather than arbitrary if two rows ever do land on
        the same instant. It is a random UUID, so it is a stability
        tiebreak, not a meaningful recency signal.
        """
        result = await self.session.execute(
            select(ScreeningResult)
            .where(ScreeningResult.deal_id == deal_id)
            .order_by(ScreeningResult.created_at.desc(), ScreeningResult.id.desc())
            .limit(1)
        )
        return result.scalars().first()
