import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

from jose import JWTError, jwt
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.exceptions import AuthenticationError


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


_INTAKE_JWT_AUDIENCE = "simpero:intake-session"
_INTAKE_JWT_ALGORITHM = "HS256"


class IntakeSessionClaims(BaseModel):
    link_id: UUID
    email: str


def encode_intake_session_jwt(link_id: UUID, email: str, ttl_minutes: int = 30) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "link_id": str(link_id),
            "email": email,
            "aud": _INTAKE_JWT_AUDIENCE,
            "iat": now,
            "exp": now + timedelta(minutes=ttl_minutes),
        },
        settings.intake_session_jwt_secret,
        algorithm=_INTAKE_JWT_ALGORITHM,
    )


def decode_intake_session_jwt(token: str) -> IntakeSessionClaims:
    settings = get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.intake_session_jwt_secret,
            algorithms=[_INTAKE_JWT_ALGORITHM],
            audience=_INTAKE_JWT_AUDIENCE,
        )
    except JWTError as exc:
        raise AuthenticationError(f"Intake session token verification failed: {exc}") from exc
    return IntakeSessionClaims(link_id=claims["link_id"], email=claims["email"])
