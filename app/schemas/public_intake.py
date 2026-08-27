from pydantic import EmailStr

from app.schemas.common import CamelModel


class IntakeEmailVerifyRequest(CamelModel):
    email: EmailStr


class IntakeSessionResponse(CamelModel):
    session_token: str
