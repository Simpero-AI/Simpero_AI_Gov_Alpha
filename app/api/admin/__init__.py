from fastapi import APIRouter

from app.api.admin import (
    context,
    invitations,
    mandates,
    members,
    organizations,
    platform_invitations,
    platform_members,
    platform_organization_delete,
)

router = APIRouter(prefix="/admin", tags=["admin"])
router.include_router(context.router)
router.include_router(invitations.router)
router.include_router(mandates.router)
router.include_router(members.router)
router.include_router(organizations.router)
router.include_router(platform_invitations.router)
router.include_router(platform_members.router)
router.include_router(platform_organization_delete.router)

__all__ = ["router"]
