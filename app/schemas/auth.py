from pydantic import BaseModel, ConfigDict, Field


class ProfileSyncRequest(BaseModel):
    """Profile fields the frontend pushes after login (Clerk's useUser() has
    them; the session token doesn't). Omitted fields are left untouched."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    email: str | None = Field(default=None, min_length=1, max_length=100)


class ProfileSyncResponse(BaseModel):
    success: bool


class LogoutResponse(BaseModel):
    success: bool


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    name: str | None
    email: str | None
    role: str
    login_method: str
    is_platform_admin: bool
