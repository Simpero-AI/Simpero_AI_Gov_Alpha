import uuid

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
