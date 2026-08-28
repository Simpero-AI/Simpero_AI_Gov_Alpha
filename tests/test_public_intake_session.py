import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import pytest
from jose import jwt
from pydantic import EmailStr, TypeAdapter
from saq.queue.redis import RedisQueue

from app.core.intake_security import (
    _INTAKE_JWT_ALGORITHM,
    decode_intake_session_jwt,
    sha256_hex,
)
from app.jobs.queue import get_queue
from app.main import app

_NOT_FOUND_BODY = {"detail": "Not found"}
_EMAIL_ADAPTER = TypeAdapter(EmailStr)


@pytest.fixture(autouse=True)
async def _clear_rate_limit_keys_around_every_test(clear_rate_limit_keys):
    """httpx.ASGITransport (used by _post_session below) defaults every test
    to the same synthetic client address, so without clearing rate-limit keys
    between tests, cumulative request counts across this file's tests would
    spuriously trip the P3-12 429 within a single run. Local + autouse
    (unlike conftest.py's own clear_rate_limit_keys) so only this module pays
    the cost."""
    yield


async def _post_session(token: str, email: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(f"/api/public/intake/{token}/session", json={"email": email})


@pytest.fixture
def link_factory(owner_conn, org_a_id, user_a_id, test_org_id):
    """Seeds a deal_intake_link row with a caller-chosen status/expiry, mirroring
    conftest.py's pending_link_with_token but parameterized so this module can
    build the expired/revoked/submitted rows the byte-identical-404 test needs.

    Each call gets its OWN deal, deliberately -- ux_deal_intake_link_pending_deal
    allows at most one 'pending' link per deal, and this fixture is used
    alongside pending_link_with_token (which already owns a pending link on its
    own deal) in the same test, so sharing a deal would collide on that index.
    """
    created_link_ids: list[str] = []
    created_deal_ids: list[str] = []

    def _make(*, status: str = "pending", expires_at: datetime | None = None) -> dict:
        raw_token = secrets.token_urlsafe(32)
        token_hash = sha256_hex(raw_token)
        expiry = expires_at or (datetime.now(UTC) + timedelta(days=7))
        with owner_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
                (org_a_id, "link_factory deal"),
            )
            deal_id = str(cur.fetchone()[0])
            created_deal_ids.append(deal_id)
            cur.execute(
                "INSERT INTO deal_intake_link "
                "(org_id, clerk_org_id, deal_id, token_hash, recipient_email, expires_at, "
                "created_by_user_id, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    org_a_id,
                    test_org_id,
                    deal_id,
                    token_hash,
                    "recipient@org-a.example",
                    expiry,
                    user_a_id,
                    status,
                ),
            )
            link_id = str(cur.fetchone()[0])
        created_link_ids.append(link_id)
        return {"id": link_id, "raw_token": raw_token}

    yield _make

    with owner_conn.cursor() as cur:
        for link_id in created_link_ids:
            cur.execute("DELETE FROM deal_intake_link WHERE id = %s", (link_id,))
        for deal_id in created_deal_ids:
            cur.execute("DELETE FROM deals WHERE id = %s", (deal_id,))


def _audit_rows(owner_conn, link_id: str, event_type: str) -> list[tuple]:
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT actor_email, payload FROM human_audit_log "
            "WHERE event_type = %s AND payload ->> 'link_id' = %s",
            (event_type, link_id),
        )
        return cur.fetchall()


async def test_correct_email_case_varied_issues_session_and_audits(
    pending_link_with_token, owner_conn
):
    link = pending_link_with_token
    submitted_email = "RECIPIENT@ORG-A.EXAMPLE"
    # pydantic's EmailStr normalizes the domain to lowercase (case-insensitive
    # per DNS) while preserving local-part case -- that's what actually reaches
    # the route handler as body.email, so it's what claims/audit rows carry.
    normalized_email = str(_EMAIL_ADAPTER.validate_python(submitted_email))

    resp = await _post_session(link["raw_token"], submitted_email)

    assert resp.status_code == 200
    body = resp.json()
    claims = decode_intake_session_jwt(body["sessionToken"])
    assert str(claims.link_id) == link["id"]
    assert claims.email == normalized_email

    rows = _audit_rows(owner_conn, link["id"], "intake_email_attempt_succeeded")
    assert len(rows) == 1
    assert rows[0][0] == normalized_email


async def test_wrong_email_404s_bumps_failed_attempt_and_audits(
    pending_link_with_token, owner_conn
):
    link = pending_link_with_token
    wrong_email = "someone-else@org-a.example"

    resp = await _post_session(link["raw_token"], wrong_email)

    assert resp.status_code == 404
    assert resp.json() == _NOT_FOUND_BODY

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT failed_attempts, last_attempt_at FROM deal_intake_link WHERE id = %s",
            (link["id"],),
        )
        failed_attempts, last_attempt_at = cur.fetchone()
    assert failed_attempts == 1
    assert last_attempt_at is not None

    rows = _audit_rows(owner_conn, link["id"], "intake_email_attempt_failed")
    assert len(rows) == 1
    assert rows[0][0] == wrong_email


async def test_mismatch_still_404s_and_bumps_attempt_with_constant_time_compare(
    pending_link_with_token, owner_conn
):
    """Regression for the hmac.compare_digest swap -- the mismatch path's
    observable behavior (404, failed_attempts bump, audit row) must be
    unchanged by switching from != to a constant-time comparison."""
    link = pending_link_with_token
    wrong_email = "someone-else@org-a.example"

    resp = await _post_session(link["raw_token"], wrong_email)

    assert resp.status_code == 404
    assert resp.json() == _NOT_FOUND_BODY

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT failed_attempts FROM deal_intake_link WHERE id = %s",
            (link["id"],),
        )
        (failed_attempts,) = cur.fetchone()
    assert failed_attempts == 1

    rows = _audit_rows(owner_conn, link["id"], "intake_email_attempt_failed")
    assert len(rows) == 1
    assert rows[0][0] == wrong_email


async def test_repeat_mismatches_increment_with_no_lockout(pending_link_with_token, owner_conn):
    link = pending_link_with_token

    for _ in range(3):
        resp = await _post_session(link["raw_token"], "still-wrong@org-a.example")
        # No lockout: every attempt gets the same 404, never a different
        # status code -- rate-limiting/lockout is P3-12, out of scope here.
        assert resp.status_code == 404

    with owner_conn.cursor() as cur:
        cur.execute("SELECT failed_attempts FROM deal_intake_link WHERE id = %s", (link["id"],))
        (failed_attempts,) = cur.fetchone()
    assert failed_attempts == 3


async def test_lockout_after_threshold_404s_even_correct_email(pending_link_with_token, owner_conn):
    link = pending_link_with_token

    for _ in range(5):
        resp = await _post_session(link["raw_token"], "still-wrong@org-a.example")
        assert resp.status_code == 404

    with owner_conn.cursor() as cur:
        cur.execute("SELECT failed_attempts FROM deal_intake_link WHERE id = %s", (link["id"],))
        (failed_attempts,) = cur.fetchone()
    assert failed_attempts == 5

    # This test's own 6-request sequence (5 above + 1 below) would otherwise
    # trip the unrelated P3-12 IP throttle (5 req/10s) against ASGITransport's
    # single synthetic client address -- clear it so this test exercises only
    # the DB-level lockout under test, not IP throttling.
    redis = cast(RedisQueue, get_queue()).redis
    async for key in redis.scan_iter("ratelimit:*"):
        await redis.delete(key)

    # 6th attempt, this time with the CORRECT email -- lockout is
    # unconditional and must still 404, byte-identical to the 404-only
    # contract, and must NOT bump failed_attempts further.
    locked_out_resp = await _post_session(link["raw_token"], "recipient@org-a.example")
    assert locked_out_resp.status_code == 404
    assert locked_out_resp.content == b'{"detail":"Not found"}'

    with owner_conn.cursor() as cur:
        cur.execute("SELECT failed_attempts FROM deal_intake_link WHERE id = %s", (link["id"],))
        (failed_attempts_after,) = cur.fetchone()
    assert failed_attempts_after == 5


async def test_byte_identical_404_across_every_failure_mode(link_factory, pending_link_with_token):
    responses: list[bytes] = []

    unknown = await _post_session("not-a-real-token", "nobody@example.com")
    responses.append(unknown.content)

    expired = link_factory(status="pending", expires_at=datetime.now(UTC) - timedelta(days=1))
    expired_resp = await _post_session(expired["raw_token"], "nobody@example.com")
    responses.append(expired_resp.content)

    revoked = link_factory(status="revoked")
    revoked_resp = await _post_session(revoked["raw_token"], "nobody@example.com")
    responses.append(revoked_resp.content)

    submitted = link_factory(status="submitted")
    submitted_resp = await _post_session(submitted["raw_token"], "nobody@example.com")
    responses.append(submitted_resp.content)

    wrong_email_resp = await _post_session(
        pending_link_with_token["raw_token"], "wrong@org-a.example"
    )
    responses.append(wrong_email_resp.content)

    for r in responses:
        assert r == responses[0]
    assert responses[0] == b'{"detail":"Not found"}'


# The issued session token being rejected by decode_clerk_jwt is covered
# structurally, not end-to-end, matching tests/test_intake_session_jwt.py's
# own precedent for the reverse direction: decode_clerk_jwt's first step is
# a live httpx call to Clerk's JWKS (app/core/security.py::_get_jwks), and
# this repo has no JWKS-mocking fixture -- building one just for this
# negative case would make the test depend on a live/reachable Clerk
# endpoint that isn't guaranteed in every environment this suite runs in.
# The property is already established: encode_intake_session_jwt signs
# HS256 with the intake secret and no "kid" header, while decode_clerk_jwt
# looks up the token's "kid" against Clerk's real RS256 keys before ever
# checking a signature -- an intake-session token has no matching kid by
# construction, so decode_clerk_jwt would reject it at that lookup step
# regardless of what the JWKS actually contains.


def test_clerk_shaped_jwt_is_rejected_by_decode_intake_session_jwt():
    """Mirrors tests/test_intake_session_jwt.py::test_clerk_shaped_jwt_is_rejected --
    a token signed with a different secret/algorithm/audience standing in for
    a Clerk-issued RS256/JWKS token."""
    from app.core.exceptions import AuthenticationError

    wrong_secret_token = jwt.encode(
        {"link_id": str(uuid.uuid4()), "email": "test@example.com", "aud": "clerk"},
        "some-other-secret",
        algorithm=_INTAKE_JWT_ALGORITHM,
    )
    with pytest.raises(AuthenticationError):
        decode_intake_session_jwt(wrong_secret_token)
