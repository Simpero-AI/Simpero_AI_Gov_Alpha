import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deal_intake_link import DealIntakeLink
from app.repo.BaseRepo import BaseRepo


class IntakeLinkRepo(BaseRepo[DealIntakeLink, dict]):
    def __init__(self, db: AsyncSession):
        super().__init__(db)

    async def create(self, data: dict, **kwargs: object) -> DealIntakeLink:
        link = DealIntakeLink(**data)
        self.session.add(link)
        return link

    async def get_by_id(self, id: object) -> DealIntakeLink | None:
        return await self.session.get(DealIntakeLink, id)

    async def get_by_token_hash(self, token_hash: str) -> DealIntakeLink | None:
        result = await self.session.execute(
            select(DealIntakeLink).where(DealIntakeLink.token_hash == token_hash)
        )
        return result.scalars().first()

    async def bump_failed_attempt(self, link_id: uuid.UUID) -> None:
        await self.session.execute(
            update(DealIntakeLink)
            .where(DealIntakeLink.id == link_id)
            .values(failed_attempts=DealIntakeLink.failed_attempts + 1, last_attempt_at=func.now())
            .execution_options(synchronize_session=False)
        )
