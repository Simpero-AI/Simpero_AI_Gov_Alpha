from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deal_intake_question import DealIntakeQuestion
from app.repo.BaseRepo import BaseRepo


class DealIntakeQuestionRepo(BaseRepo[DealIntakeQuestion, dict]):
    """Global reference table -- no org scoping. Shared by the product
    portal's read-only lookup (list_active, app/api/intake_questions.py,
    P2-03) and the admin portal's CRUD (app/api/admin/intake_questions.py,
    P2-02) -- same precedent as MandateCategoryRepo."""

    def __init__(self, db: AsyncSession):
        super().__init__(db)

    async def create(self, data: dict, **kwargs: object) -> DealIntakeQuestion:
        question = DealIntakeQuestion(**data)
        self.session.add(question)
        return question

    async def get_by_id(self, id: object) -> DealIntakeQuestion | None:
        return await self.session.get(DealIntakeQuestion, id)

    async def list_active(self) -> list[DealIntakeQuestion]:
        """The set a new link's questions_snapshot is built from (P3-01).
        Excludes deactivated rows; existing snapshots hold their own copy of
        the question text and never re-read this table, so deactivating a
        row here cannot change what a past snapshot shows."""
        result = await self.session.execute(
            select(DealIntakeQuestion)
            .where(DealIntakeQuestion.is_active.is_(True))
            .order_by(DealIntakeQuestion.display_order)
        )
        return list(result.scalars().all())
