from typing import Literal

from app.schemas.common import CamelModel


class UpdateMemberRoleRequest(CamelModel):
    role: Literal["member", "admin"]


class OrgMemberResponse(CamelModel):
    """Member view for both the org-admin's own-org members endpoints (GET
    /members) and the platform-admin cross-org endpoints (GET
    /organizations/{clerk_org_id}/members) — sourced from Clerk's membership
    API merged with locally-soft-deleted `users` rows (see list_org_members /
    list_members). `id` is the Clerk org membership id for live members, or
    the `clerk_user_id` for a locally-inactive row with no Clerk membership
    left."""

    id: str
    user_id: str
    name: str | None
    email: str | None
    role: str
    status: Literal["active", "inactive"]
