import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.data_source import DataSource
from app.repo.BaseRepo import BaseRepo


class DataSourceRepo(BaseRepo[DataSource, dict]):
    def __init__(self, db: AsyncSession):
        super().__init__(db)

    async def create(self, data: dict, **kwargs: object) -> DataSource:
        data_source = DataSource(**data)
        self.session.add(data_source)
        return data_source

    async def get_by_id(self, id: object) -> DataSource | None:
        return await self.session.get(DataSource, id)

    async def list_for_deal(self, deal_id: uuid.UUID) -> list[DataSource]:
        """RLS scopes this to the request's org — no WHERE org_id here.
        Ordered by created_at so GET /deals/{deal_id}/documents (P3-04)
        returns a stable, upload-chronological list; existing callers
        (start_analysis, start_deal_analysis) only filter/count this list
        and don't depend on any particular order."""
        result = await self.session.execute(
            select(DataSource).where(DataSource.deal_id == deal_id).order_by(DataSource.created_at)
        )
        return list(result.scalars().all())

    async def find_dedupe_candidate(self, deal_id: uuid.UUID, hash: str) -> DataSource | None:
        """Presign-time dedupe lookup. Matches `declared_sha256 OR
        fingerprint` -- not `fingerprint` alone -- because `fingerprint` stays
        NULL until the async ingest job finishes; checking it alone would miss
        a second upload of the same file started before the first upload's
        job completes. `status != 'mismatch'` excludes rows a previously
        failed integrity check: a prior upload that turned out corrupted
        should not block a fresh, legitimate re-upload of the same file.
        """
        result = await self.session.execute(
            select(DataSource)
            .where(DataSource.deal_id == deal_id)
            .where((DataSource.declared_sha256 == hash) | (DataSource.fingerprint == hash))
            .where(DataSource.status != "mismatch")
        )
        return result.scalars().first()

    async def update_status(
        self, id: uuid.UUID, status: str, fingerprint: str | None
    ) -> DataSource | None:
        """Sole write path to the mutable columns (status, fingerprint,
        status_updated_at) -- mirrors HumanAuditRepo.append()'s "sole write
        path" convention. `status_updated_at` is set server-side (`now()`)
        inside this method, not accepted as a caller-supplied parameter:
        there is exactly one legitimate transition per row, enforced by
        trg_data_source_one_way_status, so there is never a case where a
        caller needs to supply a different timestamp.
        """
        result = await self.session.execute(
            update(DataSource)
            .where(DataSource.id == id)
            .values(status=status, fingerprint=fingerprint, status_updated_at=func.now())
            .returning(DataSource)
        )
        return result.scalar_one_or_none()
