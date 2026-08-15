from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mandate import MandateOptions
from app.repo.BaseRepo import BaseRepo


class MandateOptionsRepo(BaseRepo[MandateOptions, dict]):
    """Global reference table -- no org scoping. Shared by the product
    portal's read-only lookup and the admin portal's CRUD."""

    def __init__(self, db: AsyncSession):
        super().__init__(db)

    async def create(self, data: dict, **kwargs: object) -> MandateOptions:
        option = MandateOptions(**data)
        self.session.add(option)
        return option

    async def get_by_id(self, id: object) -> MandateOptions | None:
        return await self.session.get(MandateOptions, id)

    async def list_all(self) -> list[MandateOptions]:
        """Every option, across every category -- callers group by
        category_id themselves (used to build the nested category ->
        options shape both portals return)."""
        result = await self.session.execute(select(MandateOptions).order_by(MandateOptions.option))
        return list(result.scalars().all())

    async def list_by_category(self, category_id: object) -> list[MandateOptions]:
        result = await self.session.execute(
            select(MandateOptions)
            .where(MandateOptions.category_id == category_id)
            .order_by(MandateOptions.option)
        )
        return list(result.scalars().all())

    async def update(self, id: object, data: dict) -> MandateOptions | None:
        option = await self.get_by_id(id)
        if option is None:
            return None
        for key, value in data.items():
            setattr(option, key, value)
        return option

    async def delete(self, id: object) -> bool:
        option = await self.get_by_id(id)
        if option is None:
            return False
        await self.session.delete(option)
        return True
