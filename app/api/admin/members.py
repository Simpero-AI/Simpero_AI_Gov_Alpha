from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_dependencies import _admin_actor, get_admin_db, require_org_admin
from app.core.config import get_settings
from app.models.clerk_admin_user import AdminType, ClerkAdminUser
from app.models.organisation import Users, utc_now
from app.repo.AdminUserRepo import AdminUserRepo
from app.repo.HumanAuditRepo import HumanAuditRepo
from app.schemas.admin.members import OrgMemberResponse, UpdateMemberRoleRequest
from app.schemas.common import SuccessResponse
from app.services.admin.clerk_admin import (
    clerk_error_to_http,
    fetch_clerk_user_primary_email,
    list_organization_memberships,
    remove_organization_membership,
    update_organization_membership,
)

settings = get_settings()

router = APIRouter(prefix="/members", tags=["admin"])

_CLERK_ROLE = {"member": "org:member", "admin": "org:admin"}


def _member_name(public_user_data: dict[str, Any]) -> str | None:
    parts = [public_user_data.get("first_name"), public_user_data.get("last_name")]
    name = " ".join(p for p in parts if p)
    return name or None


def _to_org_member_response(m: dict[str, Any]) -> OrgMemberResponse:
    return OrgMemberResponse(
        id=m["id"],
        user_id=m["public_user_data"]["user_id"],
        name=_member_name(m["public_user_data"]),
        email=m["public_user_data"].get("identifier"),
        role=m["role"],
        status="active",  # every live Clerk membership is definitionally active
    )


def _inactive_local_user_to_org_member_response(user: Users) -> OrgMemberResponse:
    """A removed member's Clerk membership is gone, so there's no membership
    id to reuse — the frontend's primary key for these actions is
    clerk_user_id anyway (see the PATCH/DELETE endpoints below), so it
    doubles as both `id` and `user_id` here."""
    return OrgMemberResponse(
        id=user.clerk_user_id,
        user_id=user.clerk_user_id,
        name=user.name,
        email=user.email,
        role=_CLERK_ROLE[user.role],
        status="inactive",
    )


@router.get("", response_model=list[OrgMemberResponse])
async def list_members(
    claims: dict[str, Any] = Depends(require_org_admin),
    db: AsyncSession = Depends(get_admin_db),
) -> list[OrgMemberResponse]:
    """Own-org member list: live Clerk memberships (always active, and always
    reflecting the current Clerk role even if it was changed directly in the
    Clerk Dashboard) merged with locally-soft-deleted `users` rows (removed
    members, whose Clerk membership is fully revoked and so would never
    appear in the live list otherwise). No _set_org_scope needed here, unlike
    platform_members.py::list_org_members — get_admin_db's RLS clamp already
    scopes this session to the caller's own org.

    Dedup: a local inactive row is only included if its clerk_user_id is NOT
    also in the live Clerk list — see list_org_members's docstring for why.
    """
    try:
        memberships = await list_organization_memberships(claims["tenant_id"])
    except httpx.HTTPError as exc:
        raise clerk_error_to_http(exc) from exc

    live_clerk_user_ids = {m["public_user_data"]["user_id"] for m in memberships}

    local_inactive = await db.scalars(select(Users).where(Users.status == "inactive"))

    result = [_to_org_member_response(m) for m in memberships]
    result += [
        _inactive_local_user_to_org_member_response(user)
        for user in local_inactive
        if user.clerk_user_id not in live_clerk_user_ids
    ]
    return result


@router.delete("/{clerk_user_id}", response_model=SuccessResponse)
async def remove_member(
    clerk_user_id: str,
    claims: dict[str, Any] = Depends(require_org_admin),
    db: AsyncSession = Depends(get_admin_db),
) -> SuccessResponse:
    """Member-removal semantics: primary = revoke the Clerk org membership;
    secondary = soft-delete the local `users` row (status -> 'inactive',
    deactivated_at stamped) if one exists — best-effort, since an admin-only
    identity (or a member who never logged into the product) may have none.
    Reversible by re-inviting — the row is reactivated on the re-invited
    member's next login, see _ensure_user_provisioned in
    app/core/dependencies.py.

    Target is looked up by live Clerk membership, not a local row, mirroring
    platform_members.py::remove_org_member — this also fixes the bug where an
    admin-only identity with no local `users` row could never be removed
    through this endpoint (it would 404 on the old local-only lookup).

    Since the role-change feature (PATCH /admin/members/{clerk_user_id}) lets
    a member hold an active `clerk_admin_users` row at the same time as their
    `users` row, this also deactivates that admin row if one exists — a
    deleted member must not be left with dangling `/admin` portal access."""
    if clerk_user_id == claims["user_id"]:
        raise HTTPException(status_code=403, detail="Cannot remove yourself")

    try:
        memberships = await list_organization_memberships(claims["tenant_id"])
    except httpx.HTTPError as exc:
        raise clerk_error_to_http(exc) from exc
    current = next(
        (m for m in memberships if m["public_user_data"]["user_id"] == clerk_user_id), None
    )
    if current is None:
        raise HTTPException(status_code=404, detail="Member not found")

    target_admin_row = await AdminUserRepo(db).get_by_clerk_id(clerk_user_id)
    target_is_active_admin = target_admin_row is not None and target_admin_row.status == "active"
    if target_is_active_admin:
        active_admins = await db.scalar(
            select(func.count())
            .select_from(ClerkAdminUser)
            .where(ClerkAdminUser.status == "active")
        )
        if active_admins is not None and active_admins <= 1:
            raise HTTPException(status_code=403, detail="Cannot remove the last active admin")

    try:
        await remove_organization_membership(claims["tenant_id"], clerk_user_id)
    except httpx.HTTPError as exc:
        raise clerk_error_to_http(exc) from exc

    if target_is_active_admin:
        await AdminUserRepo(db).deactivate(clerk_user_id)

    local_user = await db.scalar(select(Users).where(Users.clerk_user_id == clerk_user_id))
    if local_user is not None:
        local_user.status = "inactive"
        local_user.deactivated_at = utc_now()

    org_id, actor_id, actor_email = await _admin_actor(db, claims)
    await HumanAuditRepo(db).append(
        {
            "org_id": org_id,
            "actor_id": actor_id,
            "actor_email": actor_email,
            "event_type": "admin_member_removed",
            "payload": {
                "removed_clerk_user_id": clerk_user_id,
                "removed_email": await fetch_clerk_user_primary_email(clerk_user_id),
                "admin_role_deactivated": target_is_active_admin,
                "clerk_membership_revoked": True,
            },
        }
    )
    return SuccessResponse(success=True)


@router.patch("/{clerk_user_id}", response_model=OrgMemberResponse)
async def update_member_role(
    clerk_user_id: str,
    payload: UpdateMemberRoleRequest,
    claims: dict[str, Any] = Depends(require_org_admin),
    db: AsyncSession = Depends(get_admin_db),
) -> OrgMemberResponse:
    """Own-org role change — keeps the Clerk org membership role,
    `clerk_admin_users`, and (best-effort) `users.role` in sync. Target is
    looked up by live Clerk membership, not a local row, mirroring
    platform_members.py::update_org_member_role — this also fixes the bug
    where an admin-only identity with no local `users` row could never be
    role-changed through this endpoint. Guard order matters: self-change,
    no-op, and last-admin checks all happen before the external Clerk call
    (mirrors remove_member's discipline of never mutating Clerk
    speculatively)."""
    if clerk_user_id == claims["user_id"]:
        raise HTTPException(status_code=403, detail="Cannot change your own role")

    try:
        memberships = await list_organization_memberships(claims["tenant_id"])
    except httpx.HTTPError as exc:
        raise clerk_error_to_http(exc) from exc
    current = next(
        (m for m in memberships if m["public_user_data"]["user_id"] == clerk_user_id), None
    )
    if current is None:
        raise HTTPException(status_code=404, detail="Member not found")

    new_role = payload.role
    if _CLERK_ROLE[new_role] == current["role"]:
        return _to_org_member_response(current)

    if current["role"] == "org:admin" and new_role == "member":
        active_admins = await db.scalar(
            select(func.count())
            .select_from(ClerkAdminUser)
            .where(ClerkAdminUser.status == "active")
        )
        if active_admins is not None and active_admins <= 1:
            raise HTTPException(status_code=403, detail="Cannot demote the last active admin")

    try:
        updated = await update_organization_membership(
            claims["tenant_id"], clerk_user_id, _CLERK_ROLE[new_role]
        )
    except httpx.HTTPError as exc:
        raise clerk_error_to_http(exc) from exc

    org_id, actor_id, actor_email = await _admin_actor(db, claims)
    if new_role == "admin":
        admin_type = (
            AdminType.platform
            if claims["tenant_id"] == settings.simpero_platform_org_id
            else AdminType.client
        )
        await AdminUserRepo(db).reactivate_or_create(
            {
                "clerk_user_id": clerk_user_id,
                "clerk_org_id": claims["tenant_id"],
                "org_id": org_id,
                "email": await fetch_clerk_user_primary_email(clerk_user_id),
                "admin_type": admin_type,
            }
        )
    else:
        await AdminUserRepo(db).deactivate(clerk_user_id)

    local_user = await db.scalar(select(Users).where(Users.clerk_user_id == clerk_user_id))
    users_role_updated = local_user is not None
    if local_user is not None:
        local_user.role = new_role

    await HumanAuditRepo(db).append(
        {
            "org_id": org_id,
            "actor_id": actor_id,
            "actor_email": actor_email,
            "event_type": "admin_member_role_changed",
            "payload": {
                "target_clerk_user_id": clerk_user_id,
                "target_email": await fetch_clerk_user_primary_email(clerk_user_id),
                "old_role": current["role"],
                "new_role": _CLERK_ROLE[new_role],
                "clerk_membership_role_updated": True,
                "users_role_updated": users_role_updated,
            },
        }
    )
    return _to_org_member_response(updated)
