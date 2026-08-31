"""Contract tests for GET /api/public/intake/questions (P3-08).

Full app (app.main.app) over httpx.ASGITransport, same pattern as
tests/test_public_intake_session.py -- session_token is a real, verified
intake-session JWT (encode_intake_session_jwt), never a stubbed dependency
override, so RLS/the keyhole policies are genuinely exercised end to end.
"""

import json
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from jose import jwt

from app.core.intake_security import _INTAKE_JWT_ALGORITHM, encode_intake_session_jwt
from app.main import app

_QUESTIONS: list[dict[str, Any]] = [
    {
        "question_key": "q2_second",
        "prompt": "Second question",
        "help_text": None,
        "input_type": "text",
        "required": True,
        "display_order": 2,
    },
    {
        "question_key": "q1_first",
        "prompt": "First question",
        "help_text": "Some help text",
        "input_type": "text",
        "required": False,
        "display_order": 1,
    },
]


@pytest.fixture(autouse=True)
async def _clear_rate_limit_keys_around_every_test(clear_rate_limit_keys):
    """Same reasoning as tests/test_public_intake_session.py's own fixture --
    ASGITransport reuses one synthetic client address across this file's
    requests, which would otherwise spuriously trip the P3-12 IP throttle."""
    yield


def _session_token(link: dict, email: str = "recipient@org-a.example") -> str:
    return encode_intake_session_jwt(uuid.UUID(link["id"]), email)


async def _get(session_token: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(
            "/api/public/intake/questions",
            headers={"Authorization": f"Bearer {session_token}"},
        )


def _org_name(owner_conn, clerk_org_id: str) -> str:
    """The `organisation.name` actually backing `clerk_org_id` right now --
    never hardcode "Org A" for the shared `test-tenant-00000000` tenant.
    conftest.py's `org_a_id` fixture inserts it as "Org A" via `ON CONFLICT
    (clerk_org_id) DO NOTHING`, but other test files in this suite
    (test_ai_audit_log_immutability.py, test_human_audit_log_immutability.py)
    seed the SAME clerk_org_id under "Test Org" via their own local
    fixtures with no teardown -- whichever inserts first in a given test
    run wins the name. Asserting against the live value keeps this test
    correct regardless of suite-wide run order."""
    with owner_conn.cursor() as cur:
        cur.execute("SELECT name FROM organisation WHERE clerk_org_id = %s", (clerk_org_id,))
        return cur.fetchone()[0]


def _set_snapshot(owner_conn, link_id: str, questions: list[dict[str, Any]] | None) -> None:
    snapshot = None if questions is None else {"snapshot_version": 1, "questions": questions}
    with owner_conn.cursor() as cur:
        cur.execute(
            "UPDATE deal_intake_link SET questions_snapshot = %s WHERE id = %s",
            (json.dumps(snapshot) if snapshot is not None else None, link_id),
        )


@pytest.fixture
def org_b_link_with_questions(owner_conn) -> Iterator[dict]:
    """A second org's pending link with its own questions_snapshot, seeded
    via owner_conn (bypasses RLS) -- mirrors test_public_uploads_api.py's
    org_b_link_with_docs fixture."""
    org_b_clerk_id = f"test-tenant-b-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (org_b_clerk_id, "Org B"),
        )
        org_b_pk = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (org_b_pk, "Org B's deal"),
        )
        deal_id = str(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO users (org_id, role, login_method, clerk_user_id, clerk_org_id, status) "
            "VALUES (%s, 'admin', 'email', %s, %s, 'active') RETURNING id",
            (org_b_pk, f"test-user-b-{uuid.uuid4().hex[:8]}", org_b_clerk_id),
        )
        user_b_pk = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO deal_intake_link "
            "(org_id, clerk_org_id, deal_id, token_hash, recipient_email, expires_at, "
            "created_by_user_id, status, questions_snapshot) "
            "VALUES (%s, %s, %s, %s, %s, now() + interval '7 days', %s, 'pending', %s) "
            "RETURNING id",
            (
                org_b_pk,
                org_b_clerk_id,
                deal_id,
                uuid.uuid4().hex,
                "recipient@org-b.example",
                user_b_pk,
                json.dumps(
                    {
                        "snapshot_version": 1,
                        "questions": [
                            {
                                "question_key": "org_b_only",
                                "prompt": "Org B's own question",
                                "help_text": None,
                                "input_type": "text",
                                "required": True,
                                "display_order": 1,
                            }
                        ],
                    }
                ),
            ),
        )
        link_id = str(cur.fetchone()[0])

    yield {"id": link_id, "org_pk": org_b_pk, "deal_id": deal_id, "clerk_org_id": org_b_clerk_id}

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM deal_intake_link WHERE id = %s", (link_id,))
        cur.execute("DELETE FROM users WHERE id = %s", (user_b_pk,))
        cur.execute("DELETE FROM deals WHERE id = %s", (deal_id,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_b_pk,))


async def test_happy_path_returns_org_name_and_questions_in_display_order(
    pending_link_with_token, owner_conn
):
    link = pending_link_with_token
    _set_snapshot(owner_conn, link["id"], _QUESTIONS)

    resp = await _get(_session_token(link))

    assert resp.status_code == 200
    body = resp.json()
    assert body["orgName"] == _org_name(owner_conn, link["clerk_org_id"])
    assert [q["questionKey"] for q in body["questions"]] == ["q1_first", "q2_second"]
    assert body["questions"][0] == {
        "questionKey": "q1_first",
        "prompt": "First question",
        "helpText": "Some help text",
        "inputType": "text",
        "required": False,
        "displayOrder": 1,
    }


async def test_response_has_no_field_beyond_org_name_and_questions(
    pending_link_with_token, owner_conn
):
    link = pending_link_with_token
    _set_snapshot(owner_conn, link["id"], _QUESTIONS)

    resp = await _get(_session_token(link))

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"orgName", "questions"}
    for question in body["questions"]:
        assert set(question.keys()) == {
            "questionKey",
            "prompt",
            "helpText",
            "inputType",
            "required",
            "displayOrder",
        }


async def test_null_snapshot_returns_empty_questions_list(pending_link_with_token, owner_conn):
    link = pending_link_with_token
    _set_snapshot(owner_conn, link["id"], None)

    resp = await _get(_session_token(link))

    assert resp.status_code == 200
    assert resp.json()["questions"] == []


async def test_org_a_session_never_sees_org_b_questions(
    pending_link_with_token, org_b_link_with_questions, owner_conn
):
    link = pending_link_with_token
    _set_snapshot(owner_conn, link["id"], _QUESTIONS)

    resp = await _get(_session_token(link))

    assert resp.status_code == 200
    body = resp.json()
    assert body["orgName"] == _org_name(owner_conn, link["clerk_org_id"])
    keys = {q["questionKey"] for q in body["questions"]}
    assert "org_b_only" not in keys


async def test_invalid_session_token_404s(pending_link_with_token):
    bad_token = jwt.encode(
        {"link_id": str(uuid.uuid4()), "email": "nobody@example.com", "aud": "clerk"},
        "some-other-secret",
        algorithm=_INTAKE_JWT_ALGORITHM,
    )

    resp = await _get(bad_token)

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not found"}


async def test_unknown_link_id_in_valid_session_token_404s():
    """Structurally valid, correctly-signed session token, but for a link_id
    that doesn't exist -- get_public_session_db's own keyhole policy (no row
    visible) returns the same 404, never a different failure mode."""
    token = encode_intake_session_jwt(uuid.uuid4(), "nobody@example.com")

    resp = await _get(token)

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not found"}
