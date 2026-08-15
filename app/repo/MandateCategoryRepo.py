from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mandate import MandateCategory
from app.repo.BaseRepo import BaseRepo


class MandateCategoryRepo(BaseRepo[MandateCategory, dict]):
    """Global reference table -- no org scoping. Shared by the product
    portal's read-only lookup and the admin portal's CRUD (same precedent as
    HumanAuditRepo being used from both sides)."""

    def __init__(self, db: AsyncSession):
        super().__init__(db)

    async def create(self, data: dict, **kwargs: object) -> MandateCategory:
        category = MandateCategory(**data)
        self.session.add(category)
        return category

    async def get_by_id(self, id: object) -> MandateCategory | None:
        return await self.session.get(MandateCategory, id)

    async def list(self) -> list[MandateCategory]:
        result = await self.session.execute(
            select(MandateCategory).order_by(MandateCategory.category)
        )
        return list(result.scalars().all())

    async def update(self, id: object, data: dict) -> MandateCategory | None:
        category = await self.get_by_id(id)
        if category is None:
            return None
        for key, value in data.items():
            setattr(category, key, value)
        return category

    async def delete(self, id: object) -> bool:
        category = await self.get_by_id(id)
        if category is None:
            return False
        await self.session.delete(category)
        return True
