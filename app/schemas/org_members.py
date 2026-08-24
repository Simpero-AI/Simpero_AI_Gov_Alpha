from app.schemas.common import CamelModel


class OrgMemberResponse(CamelModel):
    """One row of GET /org-members -- options for the deal-creation lead
    picker. name can be null: a JIT-provisioned user who hasn't hit
    POST /auth/sync-profile yet has no name (see Users.name)."""

    id: int
    name: str | None
