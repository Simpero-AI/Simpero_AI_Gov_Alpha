from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_db
from app.repo.UserRepo import UserRepo
from app.schemas.org_members import OrgMemberResponse

router = APIRouter(prefix="/org-members", tags=["org-members"])


@router.get("", response_model=list[OrgMemberResponse])
async def list_org_members(db: AsyncSession = Depends(get_db)) -> list[OrgMemberResponse]:
    """Product-portal member list (deliberately separate from
    /api/admin/members -- see CLAUDE.md's admin/product separation) for the
    deal-creation lead picker. RLS scopes this to the caller's own org."""
    users = await UserRepo(db).list_active()
    return [OrgMemberResponse(id=user.id, name=user.name) for user in users]
