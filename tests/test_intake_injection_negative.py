"""X-03: SQL-injection-shaped input handling on the public intake surface's
free-text fields.

`app/schemas/public_intake.py`'s `AnswerInput.answer` is a plain `str` with
no format constraint -- the client-controlled free-text field that flows
into POST /api/public/intake/answers (draft save) and
POST /api/public/intake/submit (final answers), ending up in
`deal_intake_link.draft_answers` and `deal_intake_response.answers`, both
JSONB, both written through the ORM (never raw string interpolation). This
file proves an injection-shaped string in that field is handled safely
(round-trips byte-for-byte as inert text, schema untouched) rather than just
"happens not to crash". `IntakeEmailVerifyRequest.email` is `EmailStr`-typed
-- safe by construction -- but that claim gets one assertion here too.

Full app over httpx.ASGITransport, real Postgres, real RLS -- same pattern
as tests/test_public_404_contract.py and tests/test_public_submit_api.py. A
local `link_factory` fixture, duplicated rather than imported (this repo's
established precedent for small per-file test fixtures), seeded with a real
`questions_snapshot` -- unlike test_public_404_contract.py's own version,
which the docstring there notes deliberately doesn't seed one.
"""

import json
import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.core.intake_security import encode_intake_session_jwt, sha256_hex
from app.main import app

_SQLI_PAYLOADS = [
    "'; DROP TABLE deal_intake_response; --",
    "' OR '1'='1",
    "Robert'); DROP TABLE deal_intake_link;--",
]

# Handled separately from _SQLI_PAYLOADS: AnswerInput._reject_null_bytes
# (app/schemas/public_intake.py) rejects this at the schema layer with a 422
# -- Postgres' jsonb input rejects an embedded null byte outright
# (UntranslatableCharacterError), which without that validator would 500 at
# the DB layer instead of failing cleanly.
_NULL_BYTE_PAYLOAD = "test\x00payload"

_QUESTIONS: list[dict[str, Any]] = [
    {
        "question_key": "notes",
        "prompt": "Anything else we should know?",
        "help_text": None,
        "input_type": "text",
        "required": True,
        "display_order": 1,
    }
]


@pytest.fixture(autouse=True)
async def _clear_rate_limit_keys_around_every_test(clear_rate_limit_keys):
    """Same reasoning as every other public-route test file: ASGITransport
    gives every request in this file the same synthetic client address, so
    without it the P3-12 IP throttle would trip inside this file's own
    request volume."""
    yield


@pytest.fixture
def link_factory(owner_conn, org_a_id, user_a_id, test_org_id) -> Iterator[Any]:
    """Mirrors tests/test_public_404_contract.py's own link_factory, seeded
    with a real questions_snapshot -- that file's version deliberately
    doesn't seed one; this file's /answers and /submit cases need one."""
    created_link_ids: list[str] = []
    created_deal_ids: list[str] = []

    def _make(*, status: str = "pending") -> dict:
        raw_token = secrets.token_urlsafe(32)
        token_hash = sha256_hex(raw_token)
        snapshot = json.dumps({"snapshot_version": 1, "questions": _QUESTIONS})
        with owner_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
                (org_a_id, "x-03 injection-coverage deal"),
            )
            deal_id = str(cur.fetchone()[0])
            created_deal_ids.append(deal_id)
            cur.execute(
                "INSERT INTO deal_intake_link "
                "(org_id, clerk_org_id, deal_id, token_hash, recipient_email, expires_at, "
                "created_by_user_id, status, questions_snapshot) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    org_a_id,
                    test_org_id,
                    deal_id,
                    token_hash,
                    "recipient@org-a.example",
                    datetime.now(UTC) + timedelta(days=7),
                    user_a_id,
                    status,
                    snapshot,
                ),
            )
            link_id = str(cur.fetchone()[0])
        created_link_ids.append(link_id)
        return {"id": link_id, "deal_id": deal_id, "raw_token": raw_token}

    yield _make

    with owner_conn.cursor() as cur:
        for link_id in created_link_ids:
            cur.execute("DELETE FROM deal_intake_response WHERE link_id = %s", (link_id,))
            cur.execute("DELETE FROM data_source WHERE intake_link_id = %s", (link_id,))
            cur.execute("DELETE FROM deal_intake_link WHERE id = %s", (link_id,))
        for deal_id in created_deal_ids:
            cur.execute("DELETE FROM deals WHERE id = %s", (deal_id,))


def _session_token(link: dict, email: str = "recipient@org-a.example") -> str:
    return encode_intake_session_jwt(uuid.UUID(link["id"]), email)


async def _post_answers(session_token: str, answer: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/api/public/intake/answers",
            headers={"Authorization": f"Bearer {session_token}"},
            json={"answers": [{"questionKey": "notes", "answer": answer}]},
        )


async def _post_submit(session_token: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/api/public/intake/submit",
            headers={"Authorization": f"Bearer {session_token}"},
        )


async def _post_session(token: str, email: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(f"/api/public/intake/{token}/session", json={"email": email})


def _seed_data_source(owner_conn, org_pk: int, deal_id: str, link_id: str) -> None:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO data_source "
            "(org_id, deal_id, storage_key, filename, declared_sha256, intake_link_id, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'verified')",
            (
                org_pk,
                deal_id,
                f"seed/{uuid.uuid4().hex}.pdf",
                "doc.pdf",
                uuid.uuid4().hex + uuid.uuid4().hex,
                link_id,
            ),
        )


def _table_counts(owner_conn) -> tuple[int, int]:
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM deal_intake_link")
        link_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM deal_intake_response")
        response_count = cur.fetchone()[0]
    return link_count, response_count


@pytest.mark.parametrize("payload", _SQLI_PAYLOADS)
async def test_answer_injection_payload_round_trips_inert_via_draft_save(
    payload, link_factory, owner_conn
):
    link = link_factory()
    before_links, before_responses = _table_counts(owner_conn)

    resp = await _post_answers(_session_token(link), payload)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_key = {a["questionKey"]: a for a in body["answers"]}
    assert by_key["notes"]["answer"] == payload

    with owner_conn.cursor() as cur:
        cur.execute("SELECT draft_answers FROM deal_intake_link WHERE id = %s", (link["id"],))
        draft_answers = cur.fetchone()[0]
    stored = {a["question_key"]: a for a in draft_answers["answers"]}
    assert stored["notes"]["answer"] == payload

    # Both tables must still exist with the row counts unchanged by the
    # request itself (the link row already existed before this snapshot) --
    # proves the payload didn't actually do anything to the schema, not just
    # inferred from a 200 response.
    after_links, after_responses = _table_counts(owner_conn)
    assert after_links == before_links
    assert after_responses == before_responses


@pytest.mark.parametrize("payload", _SQLI_PAYLOADS)
async def test_answer_injection_payload_round_trips_inert_via_submit(
    payload, link_factory, owner_conn, org_a_id
):
    link = link_factory()
    token = _session_token(link)
    before_links, before_responses = _table_counts(owner_conn)

    resp = await _post_answers(token, payload)
    assert resp.status_code == 200, resp.text

    _seed_data_source(owner_conn, org_a_id, link["deal_id"], link["id"])

    resp = await _post_submit(token)
    assert resp.status_code == 200, resp.text

    with owner_conn.cursor() as cur:
        cur.execute("SELECT answers FROM deal_intake_response WHERE link_id = %s", (link["id"],))
        row = cur.fetchone()
    assert row is not None
    stored = {a["question_key"]: a for a in row[0]["answers"]}
    assert stored["notes"]["answer"] == payload

    after_links, after_responses = _table_counts(owner_conn)
    assert after_links == before_links
    assert after_responses == before_responses + 1


async def test_null_byte_answer_rejected_as_422_via_draft_save(link_factory, owner_conn):
    link = link_factory()
    before_links, before_responses = _table_counts(owner_conn)

    resp = await _post_answers(_session_token(link), _NULL_BYTE_PAYLOAD)

    assert resp.status_code == 422, resp.text
    with owner_conn.cursor() as cur:
        cur.execute("SELECT draft_answers FROM deal_intake_link WHERE id = %s", (link["id"],))
        draft_answers = cur.fetchone()[0]
    assert draft_answers is None

    after_links, after_responses = _table_counts(owner_conn)
    assert after_links == before_links
    assert after_responses == before_responses


async def test_null_byte_answer_rejected_as_422_via_submit(link_factory, owner_conn, org_a_id):
    link = link_factory()
    token = _session_token(link)
    _seed_data_source(owner_conn, org_a_id, link["deal_id"], link["id"])
    before_links, before_responses = _table_counts(owner_conn)

    resp = await _post_answers(token, _NULL_BYTE_PAYLOAD)
    assert resp.status_code == 422, resp.text

    after_links, after_responses = _table_counts(owner_conn)
    assert after_links == before_links
    assert after_responses == before_responses


@pytest.mark.parametrize("payload", _SQLI_PAYLOADS[:2])
async def test_email_field_rejects_injection_payload_as_422(payload, link_factory):
    """A real, valid raw_token is required here -- FastAPI resolves
    Depends(get_public_link_db) (the token lookup) before it validates the
    request body, so a garbage token would 404 before the email field is
    ever checked, proving nothing about EmailStr validation."""
    link = link_factory()
    resp = await _post_session(link["raw_token"], payload)
    assert resp.status_code == 422
