from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deal_intake_question import DealIntakeQuestion
from app.repo.BaseRepo import BaseRepo


class DealIntakeQuestionRepo(BaseRepo[DealIntakeQuestion, dict]):
    """Global reference table -- no org scoping. Admin portal is the sole
    write path (app/api/admin/intake_questions.py, P2-02); the product-side
    read (P2-03) reuses list_active."""

    def __init__(self, db: AsyncSession):
        super().__init__(db)

    async def create(self, data: dict, **kwargs: object) -> DealIntakeQuestion:
        question = DealIntakeQuestion(**data)
        self.session.add(question)
        return question

    async def get_by_id(self, id: object) -> DealIntakeQuestion | None:
        return await self.session.get(DealIntakeQuestion, id)

    async def list_all(self) -> list[DealIntakeQuestion]:
        """Every question, active and inactive -- the admin list needs both
        so a deactivated row stays manageable (and reactivatable) rather
        than disappearing."""
        result = await self.session.execute(
            select(DealIntakeQuestion).order_by(DealIntakeQuestion.display_order)
        )
        return list(result.scalars().all())

    async def list_active(self) -> list[DealIntakeQuestion]:
        """The set a new link's questions_snapshot is built from (P2-03).
        Excludes deactivated rows; existing snapshots hold their own copy of
        the question text and never re-read this table, so deactivating a
        row here cannot change what a past snapshot shows."""
        result = await self.session.execute(
            select(DealIntakeQuestion)
            .where(DealIntakeQuestion.is_active.is_(True))
            .order_by(DealIntakeQuestion.display_order)
        )
        return list(result.scalars().all())

    async def next_display_order(self) -> int:
        result = await self.session.execute(select(func.max(DealIntakeQuestion.display_order)))
        current_max = result.scalar_one_or_none()
        return 0 if current_max is None else current_max + 1

    async def update(self, id: object, data: dict) -> DealIntakeQuestion | None:
        question = await self.get_by_id(id)
        if question is None:
            return None
        for key, value in data.items():
            setattr(question, key, value)
        return question

    async def set_active(self, id: object, is_active: bool) -> DealIntakeQuestion | None:
        return await self.update(id, {"is_active": is_active})
