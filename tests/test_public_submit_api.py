"""Contract tests for POST /api/public/intake/submit (P3-11).

Full app (app.main.app) over httpx.ASGITransport, same pattern as
tests/test_public_answers_api.py/test_public_uploads_api.py -- session_token
is a real, verified intake-session JWT (encode_intake_session_jwt), never a
stubbed dependency override, so RLS/the keyhole policies are genuinely
exercised end to end.
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
    """Same reasoning as the other P3 public-route test files -- ASGITransport
    reuses one synthetic client address across this file's requests, which
    would otherwise spuriously trip the P3-12 IP throttle."""
    yield


def _session_token(link: dict, email: str = "recipient@org-a.example") -> str:
    return encode_intake_session_jwt(uuid.UUID(link["id"]), email)


async def _submit(session_token: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/api/public/intake/submit", params={"session_token": session_token}
        )


async def _answer(session_token: str, answers: list[dict]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/api/public/intake/answers",
            params={"session_token": session_token},
            json={"answers": answers},
        )


def _set_snapshot(owner_conn, link_id: str, questions: list[dict[str, Any]]) -> None:
    snapshot = {"snapshot_version": 1, "questions": questions}
    with owner_conn.cursor() as cur:
        cur.execute(
            "UPDATE deal_intake_link SET questions_snapshot = %s WHERE id = %s",
            (json.dumps(snapshot), link_id),
        )


def _seed_data_source(
    owner_conn, org_pk: int, deal_id: str, link_id: str | None, status: str = "verified"
) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO data_source "
            "(org_id, deal_id, storage_key, filename, declared_sha256, intake_link_id, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                org_pk,
                deal_id,
                f"seed/{uuid.uuid4().hex}.pdf",
                "doc.pdf",
                uuid.uuid4().hex + uuid.uuid4().hex,
                link_id,
                status,
            ),
        )
        return str(cur.fetchone()[0])


def _link_row(owner_conn, link_id: str) -> tuple[str, Any]:
    with owner_conn.cursor() as cur:
        cur.execute("SELECT status, submitted_at FROM deal_intake_link WHERE id = %s", (link_id,))
        return cur.fetchone()


def _response_rows(owner_conn, link_id: str) -> list[tuple]:
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT respondent_email, answers, ip_address, user_agent "
            "FROM deal_intake_response WHERE link_id = %s",
            (link_id,),
        )
        return cur.fetchall()


def _audit_rows(owner_conn, deal_id: str) -> list[tuple]:
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT actor_email, ip_address, user_agent FROM human_audit_log "
            "WHERE deal_id = %s AND event_type = 'intake_submitted'",
            (deal_id,),
        )
        return cur.fetchall()


@pytest.fixture
def _cleanup_seeded_rows(owner_conn, pending_link_with_token):
    """Deletes data_source/deal_intake_response/human_audit_log rows this
    file's tests seed or write, before pending_link_with_token's own teardown
    deletes the link row (FK on data_source.intake_link_id and
    deal_intake_response.link_id would otherwise block it)."""
    yield
    link_id = pending_link_with_token["id"]
    deal_id = pending_link_with_token["deal_id"]
    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM deal_intake_response WHERE link_id = %s", (link_id,))
        cur.execute("DELETE FROM data_source WHERE intake_link_id = %s", (link_id,))
        cur.execute(
            "DELETE FROM human_audit_log WHERE deal_id = %s AND event_type IN "
            "('intake_submitted', 'intake_email_attempt_succeeded')",
            (deal_id,),
        )


async def test_submit_happy_path(
    pending_link_with_token, owner_conn, org_a_id, _cleanup_seeded_rows
):
    link = pending_link_with_token
    _set_snapshot(owner_conn, link["id"], _QUESTIONS)
    token = _session_token(link)

    resp = await _answer(token, [{"questionKey": "use_of_proceeds", "answer": "Hiring"}])
    assert resp.status_code == 200

    _seed_data_source(owner_conn, org_a_id, link["deal_id"], link["id"])

    resp = await _submit(token)
    assert resp.status_code == 200
    body = resp.json()
    assert body["submitted"] is True
    assert "submittedAt" in body

    status, submitted_at = _link_row(owner_conn, link["id"])
    assert status == "submitted"
    assert submitted_at is not None

    rows = _response_rows(owner_conn, link["id"])
    assert len(rows) == 1
    respondent_email, answers, _ip, _ua = rows[0]
    assert respondent_email == "recipient@org-a.example"
    by_key = {a["question_key"]: a for a in answers["answers"]}
    assert by_key["use_of_proceeds"]["answer"] == "Hiring"

    audit = _audit_rows(owner_conn, link["deal_id"])
    assert len(audit) == 1
    assert audit[0][0] == "recipient@org-a.example"


async def test_submit_missing_required_answer_returns_422(
    pending_link_with_token, owner_conn, org_a_id, _cleanup_seeded_rows
):
    link = pending_link_with_token
    _set_snapshot(owner_conn, link["id"], _QUESTIONS)
    token = _session_token(link)
    _seed_data_source(owner_conn, org_a_id, link["deal_id"], link["id"])

    resp = await _submit(token)

    assert resp.status_code == 422
    assert _response_rows(owner_conn, link["id"]) == []
    status, submitted_at = _link_row(owner_conn, link["id"])
    assert status == "pending"
    assert submitted_at is None


async def test_submit_no_documents_returns_409(
    pending_link_with_token, owner_conn, _cleanup_seeded_rows
):
    link = pending_link_with_token
    _set_snapshot(owner_conn, link["id"], _QUESTIONS)
    token = _session_token(link)
    resp = await _answer(token, [{"questionKey": "use_of_proceeds", "answer": "Hiring"}])
    assert resp.status_code == 200

    resp = await _submit(token)

    assert resp.status_code == 409
    assert _response_rows(owner_conn, link["id"]) == []
    status, submitted_at = _link_row(owner_conn, link["id"])
    assert status == "pending"
    assert submitted_at is None


async def test_submit_document_gate_ignores_other_links_and_authenticated_uploads(
    pending_link_with_token, org_b_link_id, owner_conn, org_a_id, org_b_id, _cleanup_seeded_rows
):
    link = pending_link_with_token
    _set_snapshot(owner_conn, link["id"], _QUESTIONS)
    token = _session_token(link)
    resp = await _answer(token, [{"questionKey": "use_of_proceeds", "answer": "Hiring"}])
    assert resp.status_code == 200

    # A doc tied to a different link, and one with no intake_link_id at all
    # (an org-side authenticated upload) -- neither counts toward this link's
    # own document gate.
    _seed_data_source(owner_conn, org_b_id, org_b_link_id["deal_id"], org_b_link_id["id"])
    _seed_data_source(owner_conn, org_a_id, link["deal_id"], None)

    resp = await _submit(token)

    assert resp.status_code == 409
    assert _response_rows(owner_conn, link["id"]) == []


async def test_double_submit_second_call_404s(
    pending_link_with_token, owner_conn, org_a_id, _cleanup_seeded_rows
):
    link = pending_link_with_token
    _set_snapshot(owner_conn, link["id"], _QUESTIONS)
    token = _session_token(link)
    resp = await _answer(token, [{"questionKey": "use_of_proceeds", "answer": "Hiring"}])
    assert resp.status_code == 200
    _seed_data_source(owner_conn, org_a_id, link["deal_id"], link["id"])

    resp1 = await _submit(token)
    assert resp1.status_code == 200

    resp2 = await _submit(token)
    assert resp2.status_code == 404
    assert resp2.json() == {"detail": "Not found"}

    assert len(_response_rows(owner_conn, link["id"])) == 1


async def test_ip_and_user_agent_persisted(
    pending_link_with_token, owner_conn, org_a_id, _cleanup_seeded_rows
):
    link = pending_link_with_token
    _set_snapshot(owner_conn, link["id"], _QUESTIONS)
    token = _session_token(link)
    resp = await _answer(token, [{"questionKey": "use_of_proceeds", "answer": "Hiring"}])
    assert resp.status_code == 200
    _seed_data_source(owner_conn, org_a_id, link["deal_id"], link["id"])

    resp = await _submit(token)
    assert resp.status_code == 200

    _email, _answers, ip_address, user_agent = _response_rows(owner_conn, link["id"])[0]
    assert ip_address is not None
    assert user_agent is not None

    _email2, ip_address2, user_agent2 = _audit_rows(owner_conn, link["deal_id"])[0]
    assert ip_address2 is not None
    assert user_agent2 is not None


def test_unknown_client_ip_maps_to_none():
    from app.core.rate_limit_middleware import client_ip

    class _FakeRequest:
        client = None
        headers: dict[str, str] = {}

    ip = client_ip(_FakeRequest())  # type: ignore[arg-type]
    assert ip == "unknown"
    mapped = None if ip == "unknown" else ip
    assert mapped is None


@pytest.fixture
def org_b_id(owner_conn) -> Iterator[int]:
    org_b_clerk_id = f"test-tenant-b-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (org_b_clerk_id, "Org B"),
        )
        org_b_pk = cur.fetchone()[0]
    yield org_b_pk
    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_b_pk,))


@pytest.fixture
def org_b_link_id(owner_conn, org_b_id) -> Iterator[dict]:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (org_b_id, "Org B's deal"),
        )
        deal_id = str(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO users (org_id, role, login_method, clerk_user_id, clerk_org_id, status) "
            "VALUES (%s, 'admin', 'email', %s, %s, 'active') RETURNING id",
            (
                org_b_id,
                f"test-user-b-{uuid.uuid4().hex[:8]}",
                f"test-tenant-b-{uuid.uuid4().hex[:8]}",
            ),
        )
        user_b_pk = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO deal_intake_link "
            "(org_id, clerk_org_id, deal_id, token_hash, recipient_email, expires_at, "
            "created_by_user_id, status) "
            "VALUES (%s, (SELECT clerk_org_id FROM organisation WHERE id = %s), %s, %s, %s, "
            "now() + interval '7 days', %s, 'pending') RETURNING id",
            (
                org_b_id,
                org_b_id,
                deal_id,
                uuid.uuid4().hex,
                "recipient@org-b.example",
                user_b_pk,
            ),
        )
        link_id = str(cur.fetchone()[0])
    yield {"id": link_id, "deal_id": deal_id}
    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM data_source WHERE deal_id = %s", (deal_id,))
        cur.execute("DELETE FROM deal_intake_link WHERE id = %s", (link_id,))
        cur.execute("DELETE FROM users WHERE id = %s", (user_b_pk,))
        cur.execute("DELETE FROM deals WHERE id = %s", (deal_id,))


async def test_org_a_submit_does_not_affect_org_b_link(
    pending_link_with_token, org_b_link_id, owner_conn, org_a_id, _cleanup_seeded_rows
):
    """Submitting org A's own link must not touch org B's link row or create
    a deal_intake_response for it -- mirrors
    tests/test_public_answers_api.py's cross-org isolation test."""
    link = pending_link_with_token
    _set_snapshot(owner_conn, link["id"], _QUESTIONS)
    _set_snapshot(owner_conn, org_b_link_id["id"], _QUESTIONS)
    token = _session_token(link)
    resp = await _answer(token, [{"questionKey": "use_of_proceeds", "answer": "Hiring"}])
    assert resp.status_code == 200
    _seed_data_source(owner_conn, org_a_id, link["deal_id"], link["id"])

    resp = await _submit(token)

    assert resp.status_code == 200
    status, _submitted_at = _link_row(owner_conn, org_b_link_id["id"])
    assert status == "pending"
    assert _response_rows(owner_conn, org_b_link_id["id"]) == []
