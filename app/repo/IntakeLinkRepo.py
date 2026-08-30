import uuid

from sqlalchemy import func, select, text, update
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

    async def get_pending_for_deal(self, deal_id: uuid.UUID) -> DealIntakeLink | None:
        """Locked read -- load-bearing. Without FOR UPDATE, two concurrent
        generate calls on the same deal can both read the same stale-pending
        row and both try to flip it to `expired`; the second writer then hits
        the one-way-status trigger's RAISE EXCEPTION, surfacing as an
        unhandled 500 instead of a clean 409. Do not drop this lock."""
        result = await self.session.execute(
            select(DealIntakeLink)
            .where(DealIntakeLink.deal_id == deal_id)
            .where(DealIntakeLink.status == "pending")
            .with_for_update()
        )
        return result.scalars().first()

    async def get_pending_for_deal_unlocked(self, deal_id: uuid.UUID) -> DealIntakeLink | None:
        """Same shape as get_pending_for_deal but WITHOUT `.with_for_update()` --
        for read-only guard checks (e.g. start_analysis's pending-link gate)
        that must not hold a row lock across an unrelated, multi-statement
        transaction (analysis_run insert + SAQ enqueue + audit write). Do not
        reuse this for any caller that goes on to write to the returned row --
        that still needs get_pending_for_deal's lock."""
        result = await self.session.execute(
            select(DealIntakeLink)
            .where(DealIntakeLink.deal_id == deal_id)
            .where(DealIntakeLink.status == "pending")
        )
        return result.scalars().first()

    async def get_pending_by_id_for_update(self, link_id: uuid.UUID) -> DealIntakeLink | None:
        """Row-locks the link for the whole submit transaction -- closes the
        race where two concurrent /submit calls both see status='pending'
        before either writes, both pass validation, and both insert a
        deal_intake_response row. A second call blocks on this lock until the
        first commits, then sees status='submitted' and gets None here (clean
        404), same idiom as get_pending_for_deal's reissue-race lock."""
        result = await self.session.execute(
            select(DealIntakeLink)
            .where(DealIntakeLink.id == link_id)
            .where(DealIntakeLink.status == "pending")
            .with_for_update()
        )
        return result.scalars().first()

    async def mark_expired(self, link: DealIntakeLink) -> DealIntakeLink:
        """Sets status on the passed, already-tracked ORM instance. Does not
        flush -- the caller flushes explicitly so the UPDATE commits within
        the transaction before the reissue's INSERT is attempted."""
        link.status = "expired"
        return link

    async def update_draft_answers(self, link_id: uuid.UUID, draft_answers: dict) -> bool:
        """Returns False (no rows matched) when the link's own row is no
        longer visible to dd_public's intake_link_status_update policy --
        i.e. status has already left 'pending' -- rather than raising. The
        policy's USING clause requires status = 'pending' to match at all,
        so a stale call against an already-submitted/revoked/expired link
        affects zero rows here; it never reaches
        trg_deal_intake_link_one_way_status. Caller translates False to the
        same 404 every other public-route failure returns."""
        result = await self.session.execute(
            update(DealIntakeLink)
            .where(DealIntakeLink.id == link_id)
            .values(draft_answers=draft_answers)
            .returning(DealIntakeLink.id)
            .execution_options(synchronize_session=False)
        )
        return result.first() is not None

    async def lock_link(self, link_id: uuid.UUID) -> None:
        """Advisory lock serializing concurrent read-merge-write cycles
        against the same link's draft_answers -- two /answers calls for the
        SAME link (two tabs, or an auto-save racing a manual save) can each
        read the same base draft, merge in different answers, and have the
        second UPDATE silently overwrite the first's with no error to
        either caller. Mirrors DataSourceRepo.try_create_for_intake_link's
        advisory-lock pattern (P3-10) -- same idiom, deliberately a
        DIFFERENT salt (1, not 0) so this lock namespace is independent of
        that one; no reason a draft-answers save and a document-count check
        on the same link should ever contend with each other. Callers must
        re-read whatever they're about to merge AFTER this returns, not
        rely on a value fetched before the lock was acquired -- READ
        COMMITTED + holding the lock until commit means a second caller's
        read only happens after the first caller's write is visible.
        Transaction-scoped (xact, not session) -- auto-releases at
        COMMIT/ROLLBACK, safe under PgBouncer transaction pooling.
        """
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:link_id, 1))"),
            {"link_id": str(link_id)},
        )

    async def latest_for_deal(self, deal_id: uuid.UUID) -> DealIntakeLink | None:
        """The deal's most recent link row, whatever its status -- the row the
        org-side status read (P3-02) and the pipeline grid (P3-06) describe.
        Deliberately unfiltered by status: a reissue leaves the older
        `expired`/`revoked` rows behind, and both callers need the newest of
        them, not the newest *pending* one (there may be none).

        Ordered by `id` as well as `created_at` because `created_at`'s
        server_default is `now()` -- transaction time, not statement time --
        so two rows inserted in one transaction share a timestamp exactly and
        the ordering would otherwise be arbitrary between them.

        Read-only: never writes `status = 'expired'`, even for a row whose
        `expires_at` has passed. Callers pass the row through
        compute_intake_link_effective_status; only P3-01 persists that.
        """
        result = await self.session.execute(
            select(DealIntakeLink)
            .where(DealIntakeLink.deal_id == deal_id)
            .order_by(DealIntakeLink.created_at.desc(), DealIntakeLink.id.desc())
            .limit(1)
        )
        return result.scalars().first()
