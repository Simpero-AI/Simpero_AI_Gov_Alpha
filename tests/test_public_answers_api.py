"""Contract tests for POST /api/public/intake/answers (P3-09).

Full app (app.main.app) over httpx.ASGITransport, same pattern as
tests/test_public_questions_api.py -- session_token is a real, verified
intake-session JWT (encode_intake_session_jwt), never a stubbed dependency
override, so RLS/the keyhole policies are genuinely exercised end to end.
"""

import json
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from app.core.intake_security import encode_intake_session_jwt
from app.main import app

_QUESTIONS: list[dict[str, Any]] = [
    {
        "question_key": "use_of_proceeds",
        "prompt": "What are the proceeds of this raise being used for?",
        "help_text": None,
        "input_type": "text",
        "required": True,
        "display_order": 1,
    },
    {
        "question_key": "runway",
        "prompt": "How many months of runway does this give you?",
        "help_text": None,
        "input_type": "text",
        "required": False,
        "display_order": 2,
    },
]


@pytest.fixture(autouse=True)
async def _clear_rate_limit_keys_around_every_test(clear_rate_limit_keys):
    """Same reasoning as tests/test_public_questions_api.py's own fixture --
    ASGITransport reuses one synthetic client address across this file's
    requests, which would otherwise spuriously trip the P3-12 IP throttle."""
    yield


def _session_token(link: dict, email: str = "recipient@org-a.example") -> str:
    return encode_intake_session_jwt(uuid.UUID(link["id"]), email)


async def _post(session_token: str, body: dict) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/api/public/intake/answers",
            params={"session_token": session_token},
            json=body,
        )


def _set_snapshot(owner_conn, link_id: str, questions: list[dict[str, Any]] | None) -> None:
    snapshot = None if questions is None else {"snapshot_version": 1, "questions": questions}
    with owner_conn.cursor() as cur:
        cur.execute(
            "UPDATE deal_intake_link SET questions_snapshot = %s WHERE id = %s",
            (json.dumps(snapshot) if snapshot is not None else None, link_id),
        )


def _draft_answers(owner_conn, link_id: str) -> dict | None:
    with owner_conn.cursor() as cur:
        cur.execute("SELECT draft_answers FROM deal_intake_link WHERE id = %s", (link_id,))
        return cur.fetchone()[0]


def _set_status(owner_conn, link_id: str, status: str) -> None:
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE deal_intake_link SET status = %s WHERE id = %s", (status, link_id))


@pytest.fixture
def org_b_link_with_questions(owner_conn) -> Iterator[dict]:
    """A second org's pending link with its own questions_snapshot, seeded
    via owner_conn (bypasses RLS) -- mirrors test_public_questions_api.py's
    fixture of the same name."""
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


async def test_unknown_question_key_rejected_422(pending_link_with_token, owner_conn):
    link = pending_link_with_token
    _set_snapshot(owner_conn, link["id"], _QUESTIONS)

    resp = await _post(
        _session_token(link),
        {"answers": [{"questionKey": "not_a_real_key", "answer": "hi"}]},
    )

    assert resp.status_code == 422
    assert _draft_answers(owner_conn, link["id"]) is None


async def test_duplicate_question_key_rejected_422(pending_link_with_token, owner_conn):
    link = pending_link_with_token
    _set_snapshot(owner_conn, link["id"], _QUESTIONS)

    resp = await _post(
        _session_token(link),
        {
            "answers": [
                {"questionKey": "runway", "answer": "12 months"},
                {"questionKey": "runway", "answer": "18 months"},
            ]
        },
    )

    assert resp.status_code == 422
    assert _draft_answers(owner_conn, link["id"]) is None


async def test_required_question_blank_rejected_422(pending_link_with_token, owner_conn):
    link = pending_link_with_token
    _set_snapshot(owner_conn, link["id"], _QUESTIONS)

    resp = await _post(
        _session_token(link),
        {"answers": [{"questionKey": "use_of_proceeds", "answer": "   "}]},
    )

    assert resp.status_code == 422
    assert _draft_answers(owner_conn, link["id"]) is None


async def test_answer_over_max_length_rejected_422(pending_link_with_token, owner_conn):
    link = pending_link_with_token
    _set_snapshot(owner_conn, link["id"], _QUESTIONS)

    resp = await _post(
        _session_token(link),
        {"answers": [{"questionKey": "runway", "answer": "x" * 4001}]},
    )

    assert resp.status_code == 422
    assert _draft_answers(owner_conn, link["id"]) is None


async def test_partial_save_merges_across_two_calls(pending_link_with_token, owner_conn):
    link = pending_link_with_token
    _set_snapshot(owner_conn, link["id"], _QUESTIONS)
    token = _session_token(link)

    resp1 = await _post(
        token, {"answers": [{"questionKey": "use_of_proceeds", "answer": "Engineering hires"}]}
    )
    assert resp1.status_code == 200

    resp2 = await _post(token, {"answers": [{"questionKey": "runway", "answer": "18 months"}]})
    assert resp2.status_code == 200

    draft = _draft_answers(owner_conn, link["id"])
    assert draft is not None
    by_key = {a["question_key"]: a for a in draft["answers"]}
    assert by_key["use_of_proceeds"] == {
        "question_key": "use_of_proceeds",
        "prompt": "What are the proceeds of this raise being used for?",
        "answer": "Engineering hires",
        "answered": True,
    }
    assert by_key["runway"] == {
        "question_key": "runway",
        "prompt": "How many months of runway does this give you?",
        "answer": "18 months",
        "answered": True,
    }

    body = resp2.json()
    body_by_key = {a["questionKey"]: a for a in body["answers"]}
    assert body_by_key["use_of_proceeds"]["answer"] == "Engineering hires"
    assert body_by_key["runway"]["answer"] == "18 months"


async def test_prompt_and_answered_are_server_derived_extra_client_fields_ignored(
    pending_link_with_token, owner_conn
):
    link = pending_link_with_token
    _set_snapshot(owner_conn, link["id"], _QUESTIONS)

    resp = await _post(
        _session_token(link),
        {
            "answers": [
                {
                    "questionKey": "runway",
                    "answer": "24 months",
                    "prompt": "client-supplied prompt should be ignored",
                    "answered": False,
                }
            ]
        },
    )

    assert resp.status_code == 200
    draft = _draft_answers(owner_conn, link["id"])
    assert draft is not None
    by_key = {a["question_key"]: a for a in draft["answers"]}
    assert by_key["runway"]["prompt"] == "How many months of runway does this give you?"
    assert by_key["runway"]["answered"] is True


@pytest.mark.parametrize("status", ["submitted", "revoked", "expired"])
async def test_non_pending_link_404s(pending_link_with_token, owner_conn, status):
    link = pending_link_with_token
    _set_snapshot(owner_conn, link["id"], _QUESTIONS)
    token = _session_token(link)
    _set_status(owner_conn, link["id"], status)

    resp = await _post(token, {"answers": [{"questionKey": "runway", "answer": "12 months"}]})

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not found"}


async def test_org_a_session_cannot_affect_org_b_draft_answers(
    pending_link_with_token, org_b_link_with_questions, owner_conn
):
    link = pending_link_with_token
    _set_snapshot(owner_conn, link["id"], _QUESTIONS)

    resp = await _post(
        _session_token(link),
        {"answers": [{"questionKey": "runway", "answer": "12 months"}]},
    )

    assert resp.status_code == 200
    assert _draft_answers(owner_conn, org_b_link_with_questions["id"]) is None
