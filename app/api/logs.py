from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_db
from app.repo.HumanAuditRepo import HumanAuditRepo
from app.schemas.logs import ActivityRowResponse, RecentActivityResponse

router = APIRouter(prefix="/logs", tags=["logs"])

# Ported from Simpero_AI_Gov_Web's src/pages/Dashboard.tsx (WARN_ACTIONS /
# CRITICAL_ACTIONS / classifyRow) — must stay in sync with that file's sets.
_WARN_ACTIONS = {
    "section_regenerate_scaffold",
    "section_regenerate_failed",
    "memo_deliverable_patched",
}
_CRITICAL_ACTIONS = {"auth_sign_out"}


def _severity(event_type: str) -> str:
    if event_type in _CRITICAL_ACTIONS:
        return "critical"
    if event_type in _WARN_ACTIONS:
        return "warn"
    return "info"


@router.get("/recent-activity", response_model=RecentActivityResponse)
async def recent_activity(
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> RecentActivityResponse:
    """logs.recentActivity. total/warnings/critical are computed over the
    whole (RLS-scoped) audit trail; `rows` is just the most recent `limit`."""
    repo = HumanAuditRepo(db)
    event_types = await repo.all_event_types()
    rows = await repo.list_recent(limit)

    return RecentActivityResponse(
        total=len(event_types),
        warnings=sum(1 for e in event_types if _severity(e) == "warn"),
        critical=sum(1 for e in event_types if _severity(e) == "critical"),
        rows=[
            ActivityRowResponse(
                id=str(row.id),
                created_at=row.created_at,
                action=row.event_type,
                session_id=str(row.session_id) if row.session_id else None,
                job_id=None,
                actor_email=row.actor_email,
                payload=row.payload,
            )
            for row in rows
        ],
    )
