from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.human_audit_log import HumanAuditLog
from app.repo.BaseRepo import BaseRepo


class HumanAuditRepo(BaseRepo[HumanAuditLog, dict]):
    """Sole write path to human_audit_log. INSERT-only, deliberately: there is
    no update/delete method here, and the database refuses both regardless
    (REVOKE UPDATE, DELETE ON human_audit_log FROM dd_app — see the migration
    that creates the table). Do not add one.
    """

    def __init__(self, db: AsyncSession):
        super().__init__(db)

    async def append(self, data: dict, **kwargs: object) -> HumanAuditLog:
        """Insert one audit row. Named `append`, not `create`, to make the
        insert-only contract explicit at call sites."""
        row = HumanAuditLog(**data)
        self.session.add(row)
        return row

    async def create(self, data: dict, **kwargs: object) -> HumanAuditLog:
        return await self.append(data, **kwargs)

    async def get_by_id(self, id: object) -> HumanAuditLog | None:
        return await self.session.get(HumanAuditLog, id)

    async def list_recent(self, limit: int) -> list[HumanAuditLog]:
        """Backs logs.recentActivity's `rows`. Append-only means reads never
        need to worry about UPDATE/DELETE racing them."""
        result = await self.session.execute(
            select(HumanAuditLog).order_by(HumanAuditLog.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def list_for_deal(self, deal_id: object, limit: int) -> list[HumanAuditLog]:
        """Backs GET /deals/{id}/audit -- the deal-scoped audit trail (Logs
        drawer's Audit Trail tab), newest first. Filters on deal_id; the id
        tiebreak keeps ordering stable when two rows share a created_at (a
        chained job can append several within one clock_timestamp()). RLS still
        scopes to the org, so this cannot read another org's rows."""
        result = await self.session.execute(
            select(HumanAuditLog)
            .where(HumanAuditLog.deal_id == deal_id)
            .order_by(HumanAuditLog.created_at.desc(), HumanAuditLog.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def all_event_types(self) -> list[str]:
        """Every event_type in the org's audit trail (RLS-scoped), for
        logs.recentActivity's total/warnings/critical counts — those are
        computed over the whole trail, not just the `limit`-ed rows.
        # ponytail: python-side classification of a flat column scan is fine
        # at this table's size; move to a SQL CASE/count if it grows large.
        """
        result = await self.session.execute(select(HumanAuditLog.event_type))
        return list(result.scalars().all())
