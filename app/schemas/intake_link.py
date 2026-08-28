from datetime import datetime

from pydantic import EmailStr

from app.schemas.common import CamelModel


class CreateIntakeLinkRequest(CamelModel):
    recipient_email: EmailStr


class CreateIntakeLinkResponse(CamelModel):
    id: str
    token: str  # raw token -- appears ONLY in this response, never again
    status: str
    expires_at: datetime
