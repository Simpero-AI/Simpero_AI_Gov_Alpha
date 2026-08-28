"""Contract tests for GET /deals/{deal_id}/intake-response (P3-05).

Mirrors tests/test_intake_link_generate.py's ApiTestClient/dependency_overrides
pattern. Rows are seeded directly as the table owner because
deal_intake_response is blanket-immutable at the database (REVOKE UPDATE,
DELETE FROM dd_app) and the only route that will ever write it -- P3-11's
public submit -- does not exist on this branch.

The acceptance criterion is the wire shape: `id`, `dealId`,
`respondentEmail`, `submittedAt`, `answers[]` of (`questionKey`, `prompt`,
`answer`, `answered`), camelCased via CamelModel, read out of a snake_case
stored blob.
"""

import hashlib
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.dependencies import get_claims
from app.main import app

_FUTURE = datetime.now(UTC) + timedelta(days=7)

# The exact stored shape from the implementation brief's "Stored shapes"
# (section 2.4) -- snake_case keys, wrapped in a schema_version envelope.
_STORED_ANSWERS = {
    "schema_version": 1,
    "answers": [
        {
            "question_key": "use_of_proceeds",
            "prompt": "What are the proceeds of this raise being used for?",
            "answer": "Roughly 60% to expand the Toronto engineering team.",
            "answered": True,
        },
        {
            "question_key": "customer_concentration",
            "prompt": "What share of revenue comes from your largest customer?",
            "answer": "",
            "answered": False,
        },
    ],
}


def _token_hash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _claims(tenant_id: str, user_id: str) -> dict[str, Any]:
    return {"tenant_id": tenant_id, "user_id": user_id, "org_role": "admin", "raw_claims": {}}


class ApiTestClient(TestClient):
    """Prepends /api -- every route is mounted there (app/main.py)."""

    def request(self, method: str, url: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(url, str) and url.startswith("/") and not url.startswith("/api/"):
            url = f"/api{url}"
        return super().request(method, url, *args, **kwargs)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with ApiTestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_claims, None)


def _authed(tenant_id: str, user_id: str) -> None:
    app.dependency_overrides[get_claims] = lambda: _claims(tenant_id, user_id)


@pytest.fixture
def seeded_org(owner_conn) -> Iterator[dict[str, Any]]:
    clerk_org_id = f"test-tenant-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (clerk_org_id, "Intake Response Test Org"),
        )
        org_pk = cur.fetchone()[0]

    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk}

    with owner_conn.cursor() as cur:
        for table in (
            "deal_intake_response",
            "human_audit_log",
            "deal_intake_link",
            "analysis_run",
            "deals",
            "users",
        ):
            cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


@pytest.fixture
def seeded_deal(owner_conn, seeded_org) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (seeded_org["org_pk"], "Intake Response Test Deal"),
        )
        return str(cur.fetchone()[0])


def _seed_link(owner_conn, org_pk: int, clerk_org_id: str, deal_id: str, seed: str) -> str:
    """deal_intake_response.link_id is a real, NOT NULL FK -- a response can
    only exist under a link."""
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (org_id, role, login_method, clerk_user_id, clerk_org_id, status) "
            "VALUES (%s, 'admin', 'email', %s, %s, 'active') RETURNING id",
            (org_pk, f"seed-user-{uuid.uuid4().hex[:8]}", clerk_org_id),
        )
        user_pk = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO deal_intake_link "
            "(org_id, clerk_org_id, deal_id, token_hash, recipient_email, expires_at, "
            "created_by_user_id, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'submitted') RETURNING id",
            (
                org_pk,
                clerk_org_id,
                deal_id,
                _token_hash(seed),
                "recipient@example.com",
                _FUTURE,
                user_pk,
            ),
        )
        return str(cur.fetchone()[0])


def _seed_response(
    owner_conn,
    org_pk: int,
    deal_id: str,
    link_id: str,
    *,
    answers: dict | None,
    respondent_email: str = "founder@example.com",
    submitted_at: datetime | None = None,
    created_at: datetime | None = None,
) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deal_intake_response "
            "(org_id, deal_id, link_id, respondent_email, answers, submitted_at, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, now())) RETURNING id",
            (
                org_pk,
                deal_id,
                link_id,
                respondent_email,
                json.dumps(answers) if answers is not None else None,
                submitted_at if submitted_at is not None else datetime.now(UTC),
                created_at,
            ),
        )
        return str(cur.fetchone()[0])


def test_returns_the_documented_wire_shape_camelcased(client, owner_conn, seeded_org, seeded_deal):
    """The ticket's acceptance criterion, asserted as an exact key set at
    both levels -- top level and per answer -- so a field added to the model
    later cannot silently widen the contract."""
    link_id = _seed_link(
        owner_conn, seeded_org["org_pk"], seeded_org["clerk_org_id"], seeded_deal, "shape"
    )
    submitted_at = datetime.now(UTC)
    response_id = _seed_response(
        owner_conn,
        seeded_org["org_pk"],
        seeded_deal,
        link_id,
        answers=_STORED_ANSWERS,
        submitted_at=submitted_at,
    )
    _authed(seeded_org["clerk_org_id"], "user-response-1")

    response = client.get(f"/deals/{seeded_deal}/intake-response")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"id", "dealId", "respondentEmail", "submittedAt", "answers"}
    assert body["id"] == response_id
    assert body["dealId"] == seeded_deal
    assert body["respondentEmail"] == "founder@example.com"
    assert body["submittedAt"] is not None

    assert len(body["answers"]) == 2
    for answer in body["answers"]:
        assert set(answer) == {"questionKey", "prompt", "answer", "answered"}

    first, second = body["answers"]
    assert first["questionKey"] == "use_of_proceeds"
    assert first["prompt"] == "What are the proceeds of this raise being used for?"
    assert first["answer"].startswith("Roughly 60%")
    assert first["answered"] is True
    # An unanswered optional question stays distinguishable from a blank
    # answer -- `answered` is read from the blob, not inferred from `answer`.
    assert second["questionKey"] == "customer_concentration"
    assert second["answer"] == ""
    assert second["answered"] is False


def test_answers_keep_the_stored_order(client, owner_conn, seeded_org, seeded_deal):
    """The snapshot's display_order is baked into the stored list's order at
    submit time; this endpoint must not re-sort it."""
    link_id = _seed_link(
        owner_conn, seeded_org["org_pk"], seeded_org["clerk_org_id"], seeded_deal, "order"
    )
    stored = {
        "schema_version": 1,
        "answers": [
            {"question_key": f"q_{i}", "prompt": f"Question {i}?", "answer": "x", "answered": True}
            for i in (3, 1, 2)
        ],
    }
    _seed_response(owner_conn, seeded_org["org_pk"], seeded_deal, link_id, answers=stored)
    _authed(seeded_org["clerk_org_id"], "user-response-2")

    body = client.get(f"/deals/{seeded_deal}/intake-response").json()

    assert [a["questionKey"] for a in body["answers"]] == ["q_3", "q_1", "q_2"]


def test_returns_the_most_recent_submission(client, owner_conn, seeded_org, seeded_deal):
    """Q5: a reissued link's submission is a NEW row, not an edit of the old
    one -- so a deal collected from twice has two rows here and the reader
    wants the newer."""
    older_link = _seed_link(
        owner_conn, seeded_org["org_pk"], seeded_org["clerk_org_id"], seeded_deal, "first-round"
    )
    newer_link = _seed_link(
        owner_conn, seeded_org["org_pk"], seeded_org["clerk_org_id"], seeded_deal, "second-round"
    )
    _seed_response(
        owner_conn,
        seeded_org["org_pk"],
        seeded_deal,
        older_link,
        answers=_STORED_ANSWERS,
        respondent_email="first@example.com",
        created_at=datetime.now(UTC) - timedelta(days=3),
    )
    _seed_response(
        owner_conn,
        seeded_org["org_pk"],
        seeded_deal,
        newer_link,
        answers=_STORED_ANSWERS,
        respondent_email="second@example.com",
    )
    _authed(seeded_org["clerk_org_id"], "user-response-3")

    body = client.get(f"/deals/{seeded_deal}/intake-response").json()

    assert body["respondentEmail"] == "second@example.com"


def test_extra_keys_in_the_stored_blob_are_not_passed_through(
    client, owner_conn, seeded_org, seeded_deal
):
    """The stored blob is ours to evolve; the wire shape is a frozen contract.
    A key added to the blob later must not appear on the wire by accident."""
    link_id = _seed_link(
        owner_conn, seeded_org["org_pk"], seeded_org["clerk_org_id"], seeded_deal, "extra-keys"
    )
    stored = {
        "schema_version": 1,
        "answers": [
            {
                "question_key": "use_of_proceeds",
                "prompt": "What are the proceeds being used for?",
                "answer": "Runway.",
                "answered": True,
                "internal_scoring_hint": "do-not-ship",
            }
        ],
    }
    _seed_response(owner_conn, seeded_org["org_pk"], seeded_deal, link_id, answers=stored)
    _authed(seeded_org["clerk_org_id"], "user-response-4")

    body = client.get(f"/deals/{seeded_deal}/intake-response").json()

    assert set(body["answers"][0]) == {"questionKey", "prompt", "answer", "answered"}


def test_null_answers_blob_yields_an_empty_list_not_a_500(
    client, owner_conn, seeded_org, seeded_deal
):
    """`answers` is a nullable column. A row with no blob is degenerate but
    reachable, and must read as "submitted, nothing recorded" rather than
    crashing the org user's Step 3."""
    link_id = _seed_link(
        owner_conn, seeded_org["org_pk"], seeded_org["clerk_org_id"], seeded_deal, "null-answers"
    )
    _seed_response(owner_conn, seeded_org["org_pk"], seeded_deal, link_id, answers=None)
    _authed(seeded_org["clerk_org_id"], "user-response-5")

    response = client.get(f"/deals/{seeded_deal}/intake-response")

    assert response.status_code == 200
    assert response.json()["answers"] == []


def test_404_when_nothing_has_been_submitted_yet(client, owner_conn, seeded_org, seeded_deal):
    """A link existing -- even a live pending one -- is not a response."""
    _seed_link(
        owner_conn, seeded_org["org_pk"], seeded_org["clerk_org_id"], seeded_deal, "no-response"
    )
    _authed(seeded_org["clerk_org_id"], "user-response-6")

    assert client.get(f"/deals/{seeded_deal}/intake-response").status_code == 404


def test_404_for_an_unknown_deal(client, seeded_org):
    _authed(seeded_org["clerk_org_id"], "user-response-7")

    assert client.get(f"/deals/{uuid.uuid4()}/intake-response").status_code == 404


def test_another_tenant_cannot_read_the_response(client, owner_conn, seeded_org, seeded_deal):
    """RLS scoping for this table is covered in depth by
    tests/test_intake_response_rls.py; this is the HTTP-layer check that the
    endpoint inherits it rather than reading around it."""
    link_id = _seed_link(
        owner_conn, seeded_org["org_pk"], seeded_org["clerk_org_id"], seeded_deal, "cross-tenant"
    )
    _seed_response(owner_conn, seeded_org["org_pk"], seeded_deal, link_id, answers=_STORED_ANSWERS)

    other_clerk_org_id = f"test-tenant-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now())",
            (other_clerk_org_id, "Other Org"),
        )
    _authed(other_clerk_org_id, "user-other-tenant")

    assert client.get(f"/deals/{seeded_deal}/intake-response").status_code == 404

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE clerk_org_id = %s", (other_clerk_org_id,))
        cur.execute("DELETE FROM organisation WHERE clerk_org_id = %s", (other_clerk_org_id,))
