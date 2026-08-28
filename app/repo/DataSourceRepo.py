import uuid

from sqlalchemy import func, select, text, update
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
        and don't depend on any particular order.

        created_at alone has no tie-breaker for rows inserted in the same
        transaction (it defaults to now(), i.e. transaction-start time, not
        clock_timestamp()) -- every write today is one document per request,
        so this doesn't happen in practice, but id as a secondary sort key
        makes the order deterministic regardless of how documents get
        inserted in the future (e.g. a batch-upload path)."""
        result = await self.session.execute(
            select(DataSource)
            .where(DataSource.deal_id == deal_id)
            .order_by(DataSource.created_at, DataSource.id)
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

    async def count_for_intake_link(self, intake_link_id: uuid.UUID) -> int:
        """Plain unlocked count -- used by both the presign-time courtesy
        check and internally by try_create_for_intake_link, after that
        method's own advisory lock is held."""
        result = await self.session.execute(
            select(func.count())
            .select_from(DataSource)
            .where(DataSource.intake_link_id == intake_link_id)
        )
        return result.scalar_one()

    async def try_create_for_intake_link(
        self, intake_link_id: uuid.UUID, data: dict, ceiling: int
    ) -> DataSource | None:
        """The real 20-file-per-link boundary (presign's own check is UX-only).
        pg_advisory_xact_lock keyed on intake_link_id serializes concurrent
        /complete calls for the SAME link so two racing requests can't both
        observe count=19 and both insert, landing at 21 -- mirrors why
        IntakeLinkRepo.get_pending_for_deal takes FOR UPDATE for the reissue
        race, just via advisory lock instead of a row lock since there's no
        existing row to lock against at insert time. Transaction-scoped (xact,
        not session) -- lock auto-releases at COMMIT/ROLLBACK, safe under
        PgBouncer transaction pooling, same discipline as SET LOCAL.
        """
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:link_id, 0))"),
            {"link_id": str(intake_link_id)},
        )
        count = await self.count_for_intake_link(intake_link_id)
        if count >= ceiling:
            return None
        data_source = DataSource(**data, intake_link_id=intake_link_id)
        self.session.add(data_source)
        return data_source

    async def count_for_intake_link_by_status(
        self, intake_link_id: uuid.UUID, statuses: tuple[str, ...] = ("pending", "verified")
    ) -> int:
        """The real 'has this recipient uploaded anything' gate for P3-11 submit
        -- deliberately intake_link_id-scoped, not deal_id-scoped, same
        reasoning as count_for_intake_link (P3-10): a deal-scoped count would
        let an org-side authenticated upload satisfy an external recipient's
        own upload requirement with zero uploads of their own."""
        result = await self.session.execute(
            select(func.count())
            .select_from(DataSource)
            .where(DataSource.intake_link_id == intake_link_id)
            .where(DataSource.status.in_(statuses))
        )
        return result.scalar_one()

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
