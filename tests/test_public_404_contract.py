"""P3-13: the 404-only failure contract, audited across the WHOLE public
intake surface, not just one route.

tests/test_public_intake_session.py already proves this for POST /session
in isolation (test_byte_identical_404_across_every_failure_mode). This file
extends the same proof to every route reachable only after a session token
exists: GET /questions, POST /answers, POST /submit,
POST /uploads/presigned-url, POST /uploads/{id}/complete -- none of which
had a byte-identical-body assertion before this ticket (each route's own
test file checks status_code == 404, never the response body itself, and
uploads.py had no 404 coverage at all).

Full app over httpx.ASGITransport, same pattern as every other public-route
test file in this repo -- real session tokens (encode_intake_session_jwt),
real Postgres, real RLS.
"""

import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from saq.queue.redis import RedisQueue

from app.core.intake_security import encode_intake_session_jwt, sha256_hex
from app.jobs.queue import get_queue
from app.main import app

_NOT_FOUND_BYTES = b'{"detail":"Not found"}'
_GARBAGE_SESSION_TOKEN = "not-a-real-jwt"


@pytest.fixture(autouse=True)
async def _clear_rate_limit_keys_around_every_test(clear_rate_limit_keys):
    """Same reasoning as every other public-route test file: ASGITransport
    gives every request in this file the same synthetic client address, so
    without clearing between tests the P3-12 IP throttle would trip inside
    this file's own request volume."""
    yield


@pytest.fixture
def link_factory(owner_conn, org_a_id, user_a_id, test_org_id) -> Iterator[Any]:
    """Mirrors tests/test_public_intake_session.py's own link_factory --
    duplicated rather than imported/moved to conftest.py, matching this
    repo's established precedent of keeping small test fixtures local to the
    file that needs the exact shape (test_public_intake_session.py's own
    version doesn't seed a questions_snapshot, which this file's /answers
    and /submit cases need)."""
    created_link_ids: list[str] = []
    created_deal_ids: list[str] = []

    def _make(*, status: str = "pending", expires_at: datetime | None = None) -> dict:
        raw_token = secrets.token_urlsafe(32)
        token_hash = sha256_hex(raw_token)
        expiry = expires_at or (datetime.now(UTC) + timedelta(days=7))
        with owner_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
                (org_a_id, "p3-13 404-contract deal"),
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


def _session_token_for(link: dict, email: str = "recipient@org-a.example") -> str:
    return encode_intake_session_jwt(uuid.UUID(link["id"]), email)


async def _get_questions(session_token: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(
            "/api/public/intake/questions",
            headers={"Authorization": f"Bearer {session_token}"},
        )


async def _post_answers(session_token: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/api/public/intake/answers",
            headers={"Authorization": f"Bearer {session_token}"},
            json={"answers": [{"questionKey": "irrelevant", "answer": "x"}]},
        )


async def _post_submit(session_token: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/api/public/intake/submit",
            headers={"Authorization": f"Bearer {session_token}"},
        )


async def _post_presigned_url(session_token: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/api/public/intake/uploads/presigned-url",
            headers={"Authorization": f"Bearer {session_token}"},
            json={"filename": "financials.xlsx", "size": 1024, "declaredSha256": "a" * 64},
        )


async def _post_complete(session_token: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            f"/api/public/intake/uploads/{uuid.uuid4()}/complete",
            headers={"Authorization": f"Bearer {session_token}"},
            json={"filename": "financials.xlsx", "declaredSha256": "a" * 64},
        )


# Every session-authenticated route, in one place -- if a new public route
# is ever added and forgotten here, this list is the thing to extend.
_SESSION_ROUTES = [
    ("questions", _get_questions),
    ("answers", _post_answers),
    ("submit", _post_submit),
    ("presigned-url", _post_presigned_url),
    ("complete", _post_complete),
]


@pytest.mark.parametrize("route_name,caller", _SESSION_ROUTES)
async def test_garbage_session_token_404s_byte_identical(route_name, caller):
    resp = await caller(_GARBAGE_SESSION_TOKEN)
    assert resp.status_code == 404, route_name
    assert resp.content == _NOT_FOUND_BYTES, route_name


@pytest.mark.parametrize("route_name,caller", _SESSION_ROUTES)
async def test_session_token_for_unknown_link_id_404s_byte_identical(route_name, caller):
    """A structurally valid, correctly-signed session JWT naming a link_id
    that was never a real row -- distinct from a garbage/malformed token,
    and distinct from a revoked/expired *real* link. get_public_session_db's
    own get_by_id(claims.link_id) returns None here."""
    fake_link_id = uuid.uuid4()
    token = encode_intake_session_jwt(fake_link_id, "nobody@example.com")
    resp = await caller(token)
    assert resp.status_code == 404, route_name
    assert resp.content == _NOT_FOUND_BYTES, route_name


@pytest.mark.parametrize("route_name,caller", _SESSION_ROUTES)
async def test_session_token_for_expired_link_404s_byte_identical(route_name, caller, link_factory):
    """intake_session_lookup's USING clause checks expires_at > now()
    unconditionally, regardless of the stored status column -- a pending row
    past its own expiry is invisible via the session path exactly like the
    token path (P3-07's own byte-identical test already covers /session
    itself; this is the same property for every route after it)."""
    expired = link_factory(status="pending", expires_at=datetime.now(UTC) - timedelta(days=1))
    resp = await caller(_session_token_for(expired))
    assert resp.status_code == 404, route_name
    assert resp.content == _NOT_FOUND_BYTES, route_name


@pytest.mark.parametrize("route_name,caller", _SESSION_ROUTES)
async def test_session_token_for_revoked_link_404s_byte_identical(route_name, caller, link_factory):
    """intake_session_lookup only admits status IN ('pending', 'submitted')
    -- 'revoked' is invisible via the session path, same as the token path."""
    revoked = link_factory(status="revoked")
    resp = await caller(_session_token_for(revoked))
    assert resp.status_code == 404, route_name
    assert resp.content == _NOT_FOUND_BYTES, route_name


async def test_all_failure_bodies_across_the_whole_surface_are_byte_identical(link_factory):
    """The single strongest form of this contract: every failure-mode/route
    combination above, plus POST /session's own established failure modes,
    collapsed into one list and compared against each other directly --
    not just each individually asserted against the same literal, but
    proven mutually identical in one place."""
    bodies: list[tuple[str, str, bytes]] = []
    redis = cast(RedisQueue, get_queue()).redis

    async def _clear_throttle() -> None:
        # This test alone issues 5 routes x 4 modes = 20 requests against
        # ASGITransport's single synthetic client address -- clearing
        # between every call keeps the P3-12 IP throttle (5 req/10s) from
        # tripping mid-test and masking the actual 404-body assertion this
        # test exists to make. Not a product bug; see this test's own
        # discovery of the collision (route "answers", mode "unknown_link"
        # hit 429 before this fix) documented in the status doc's Flagged
        # section.
        async for key in redis.scan_iter("ratelimit:*"):
            await redis.delete(key)

    for route_name, caller in _SESSION_ROUTES:
        await _clear_throttle()
        bodies.append((route_name, "garbage", (await caller(_GARBAGE_SESSION_TOKEN)).content))

        await _clear_throttle()
        fake_link_id = uuid.uuid4()
        token = encode_intake_session_jwt(fake_link_id, "nobody@example.com")
        bodies.append((route_name, "unknown_link", (await caller(token)).content))

        await _clear_throttle()
        expired = link_factory(status="pending", expires_at=datetime.now(UTC) - timedelta(days=1))
        bodies.append((route_name, "expired", (await caller(_session_token_for(expired))).content))

        await _clear_throttle()
        revoked = link_factory(status="revoked")
        bodies.append((route_name, "revoked", (await caller(_session_token_for(revoked))).content))

    for route_name, mode, body in bodies:
        assert body == bodies[0][2], f"{route_name}/{mode}: {body!r} != {bodies[0][2]!r}"
    assert bodies[0][2] == _NOT_FOUND_BYTES
