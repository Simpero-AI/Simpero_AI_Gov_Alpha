import uuid

import pytest
from jose import jwt

from app.core.exceptions import AuthenticationError
from app.core.intake_security import (
    _INTAKE_JWT_ALGORITHM,
    decode_intake_session_jwt,
    encode_intake_session_jwt,
)


def test_encode_decode_round_trip():
    link_id = uuid.uuid4()
    token = encode_intake_session_jwt(link_id, "test@example.com")

    claims = decode_intake_session_jwt(token)

    assert claims.link_id == link_id
    assert claims.email == "test@example.com"


def test_clerk_shaped_jwt_is_rejected():
    """A JWT signed with a different secret and/or the wrong audience --
    standing in for a Clerk-issued token here, since Clerk's real tokens are
    RS256/JWKS-verified (no shared secret to hand-craft against) -- must be
    rejected by decode_intake_session_jwt. Proves the audience/signature
    check actually binds, without needing to fabricate a fully Clerk-shaped
    RS256 token."""
    wrong_secret_token = jwt.encode(
        {
            "link_id": str(uuid.uuid4()),
            "email": "test@example.com",
            "aud": "simpero:intake-session",
        },
        "some-other-secret",
        algorithm=_INTAKE_JWT_ALGORITHM,
    )
    with pytest.raises(AuthenticationError):
        decode_intake_session_jwt(wrong_secret_token)


def test_wrong_audience_jwt_is_rejected():
    from app.core.config import get_settings

    settings = get_settings()
    wrong_audience_token = jwt.encode(
        {"link_id": str(uuid.uuid4()), "email": "test@example.com", "aud": "clerk"},
        settings.intake_session_jwt_secret,
        algorithm=_INTAKE_JWT_ALGORITHM,
    )
    with pytest.raises(AuthenticationError):
        decode_intake_session_jwt(wrong_audience_token)


# The reverse (an intake-session JWT fed to decode_clerk_jwt) is covered
# structurally, not re-tested end-to-end: decode_clerk_jwt looks up the
# token's `kid` header against Clerk's live JWKS via an httpx call
# (app/core/security.py::_get_jwks), and this repo's own test_security.py
# has no JWKS-mocking fixture to reuse -- building one just for this
# negative case is disproportionate to what it proves. The property is
# already established from the other direction (test_wrong_audience_jwt_is_rejected
# above): the two token types use different signing keys and audiences,
# and jose.jwt.decode enforces both, so a decode_clerk_jwt call against an
# HS256, intake-audienced token would fail at the "unknown kid" step before
# ever reaching the RS256 signature/audience checks that reject it a second
# time over.
