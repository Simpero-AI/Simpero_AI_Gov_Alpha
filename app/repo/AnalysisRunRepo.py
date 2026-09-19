import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analysis_run import AnalysisRun
from app.repo.BaseRepo import BaseRepo

_ACTIVE_STATUSES = ("queued", "in_progress")
_TERMINAL_STATUSES = ("successful", "failed")


class AnalysisRunRepo(BaseRepo[AnalysisRun, dict]):
    def __init__(self, db: AsyncSession):
        super().__init__(db)

    async def create(self, data: dict, **kwargs: object) -> AnalysisRun:
        run = AnalysisRun(**data)
        self.session.add(run)
        return run

    async def get_by_id(self, id: object) -> AnalysisRun | None:
        return await self.session.get(AnalysisRun, id)

    async def latest_for_deal(self, deal_id: uuid.UUID) -> AnalysisRun | None:
        result = await self.session.execute(
            select(AnalysisRun)
            .where(AnalysisRun.deal_id == deal_id)
            .order_by(AnalysisRun.started_at.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def latest_by_job_name(self, deal_id: uuid.UUID, job_name: str) -> AnalysisRun | None:
        """Most recent run of one job_name for a deal -- used by the status
        endpoint to find the parsing run a verification run chained from
        (they're different rows; a verification row carries no FK back to
        it), so each step's own real started_at/ended_at can be shown
        without fabricating anything."""
        result = await self.session.execute(
            select(AnalysisRun)
            .where(AnalysisRun.deal_id == deal_id)
            .where(AnalysisRun.job_name == job_name)
            .order_by(AnalysisRun.started_at.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def latest_for_deals(self, deal_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, AnalysisRun]:
        """Every listed deal's most recent run (any job_name), in ONE query,
        keyed by deal_id -- the batched counterpart of latest_for_deal for the
        Live Pipeline grid, which otherwise ran this per row (N+1). DISTINCT ON
        (deal_id) with the same started_at DESC ordering returns the newest row
        per deal; `id` DESC is the deterministic tie-break for two runs sharing a
        started_at. Deals with no run are simply absent from the dict.

        Same bind-parameter ceiling as IntakeLinkRepo.latest_for_deals: deal_ids
        is spread into an IN (...) list and DealRepo.list() applies no limit, so a
        large enough org hits Postgres's 65535-parameter wall -- fine at Alpha
        volume, and strictly better than the per-row loop it replaces."""
        if not deal_ids:
            return {}
        result = await self.session.execute(
            select(AnalysisRun)
            .where(AnalysisRun.deal_id.in_(deal_ids))
            .distinct(AnalysisRun.deal_id)
            .order_by(
                AnalysisRun.deal_id,
                AnalysisRun.started_at.desc(),
                AnalysisRun.id.desc(),
            )
        )
        return {run.deal_id: run for run in result.scalars().all()}

    async def latest_by_job_name_for_deals(
        self, deal_ids: Sequence[uuid.UUID], job_name: str
    ) -> dict[uuid.UUID, AnalysisRun]:
        """Every listed deal's most recent run of one job_name, in ONE query,
        keyed by deal_id -- the batched counterpart of latest_by_job_name, so the
        pipeline grid can resolve each deal's parsing/verification chain rows
        without a query per deal per job. Same DISTINCT ON idiom and bind-limit
        caveat as latest_for_deals."""
        if not deal_ids:
            return {}
        result = await self.session.execute(
            select(AnalysisRun)
            .where(AnalysisRun.deal_id.in_(deal_ids))
            .where(AnalysisRun.job_name == job_name)
            .distinct(AnalysisRun.deal_id)
            .order_by(
                AnalysisRun.deal_id,
                AnalysisRun.started_at.desc(),
                AnalysisRun.id.desc(),
            )
        )
        return {run.deal_id: run for run in result.scalars().all()}

    async def active_for_deal(self, deal_id: uuid.UUID) -> AnalysisRun | None:
        """Fast-path check for a friendly 409 -- uq_analysis_run_active (the
        partial unique index) is the actual double-submit guarantee; this
        SELECT can't catch two concurrent requests racing past it."""
        result = await self.session.execute(
            select(AnalysisRun)
            .where(AnalysisRun.deal_id == deal_id)
            .where(AnalysisRun.status.in_(_ACTIVE_STATUSES))
        )
        return result.scalars().first()

    async def update_progress(
        self,
        id: uuid.UUID,
        *,
        status: str | None = None,
        parse_jobs: list | None = None,
        error_message: str | None = None,
        job_comments: list | None = None,
    ) -> AnalysisRun:
        """Sole write path to the run's mutable columns. SELECT ... FOR
        UPDATE locks the row for the rest of this transaction before
        applying the given fields, so a redelivered/overlapping worker
        attempt serializes against this write instead of losing it.

        `ended_at` is never a caller-supplied parameter -- like
        `DataSourceRepo.update_status`'s `status_updated_at`, it's stamped
        server-side, automatically, the one time `status` is set to a
        terminal value (`successful`/`failed`)."""
        run = (
            await self.session.execute(
                select(AnalysisRun).where(AnalysisRun.id == id).with_for_update()
            )
        ).scalar_one()
        if status is not None:
            run.status = status
            if status in _TERMINAL_STATUSES:
                run.ended_at = func.now()
        if parse_jobs is not None:
            run.parse_jobs = parse_jobs
        if error_message is not None:
            run.error_message = error_message
        if job_comments is not None:
            run.job_comments = job_comments
        return run
