from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_claims, get_db
from app.models.clerk_admin_user import AdminType
from app.models.organisation import Users
from app.repo.AdminUserRepo import AdminUserRepo
from app.repo.HumanAuditRepo import HumanAuditRepo
from app.repo.UserRepo import UserRepo
from app.schemas.auth import LogoutResponse, ProfileSyncRequest, ProfileSyncResponse, UserResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me", response_model=UserResponse)
async def me(
    claims: dict[str, Any] = Depends(get_claims),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """The caller's own user row. get_db JIT-provisions it on first login,
    so a valid token always finds exactly one row here."""
    user = await UserRepo(db).get_by_clerk_id(claims["user_id"])
    assert user is not None  # get_db JIT-provisions this row before the handler runs
    await HumanAuditRepo(db).append(
        {
            "org_id": user.org_id,
            "actor_id": claims["user_id"],
            "actor_email": user.email,
            "event_type": "auth_login",
        }
    )
    admin = await AdminUserRepo(db).get_by_clerk_id(claims["user_id"])
    is_platform_admin = (
        admin is not None and admin.admin_type == AdminType.platform and admin.status == "active"
    )
    return UserResponse(
        id=user.id,
        org_id=user.org_id,
        name=user.name,
        email=user.email,
        role=user.role,
        login_method=user.login_method,
        is_platform_admin=is_platform_admin,
    )


@router.post("/sync-profile", response_model=ProfileSyncResponse)
async def sync_profile(
    payload: ProfileSyncRequest,
    claims: dict[str, Any] = Depends(get_claims),
    db: AsyncSession = Depends(get_db),
) -> ProfileSyncResponse:
    """
    Fill in name/email on the caller's Users row.

    Clerk's session token carries no name/email, so get_db JIT-creates the row
    with NULLs; the frontend calls this once after sign-in (and again whenever
    the Clerk profile changes) — same pattern as simpero_GOV_AI's auth.syncProfile.
    """
    fields = payload.model_dump(exclude_unset=True, exclude_none=True)
    if fields:
        await db.execute(
            update(Users).where(Users.clerk_user_id == claims["user_id"]).values(**fields)
        )
    return ProfileSyncResponse(success=True)


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    claims: dict[str, Any] = Depends(get_claims),
    db: AsyncSession = Depends(get_db),
) -> LogoutResponse:
    """Clerk's signOut() is entirely client-side (it just drops the session
    token) — this endpoint's only job is the compliance trail: one
    `auth_sign_out` row via HumanAuditRepo."""
    user = await UserRepo(db).get_by_clerk_id(claims["user_id"])
    assert user is not None  # get_db JIT-provisions this row before the handler runs
    await HumanAuditRepo(db).append(
        {
            "org_id": user.org_id,
            "actor_id": claims["user_id"],
            "actor_email": user.email,
            "event_type": "auth_sign_out",
        }
    )
    return LogoutResponse(success=True)
