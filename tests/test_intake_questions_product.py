"""GET /api/intake-questions (P2-03) -- the product-side read the org
user's Step 1 (P3-01) will snapshot from at link-generation time.

Same harness as tests/test_mandate_endpoints.py -- duplicated fixtures
rather than shared, per that module's own precedent.

deal_intake_questions is a global reference table (no org_id), so the
autouse cleanup fixture truncates it around every test rather than relying
on org-scoped teardown.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.dependencies import get_claims
from app.main import app


def _claims(tenant_id: str, user_id: str, org_role: str = "member") -> dict[str, Any]:
    return {"tenant_id": tenant_id, "user_id": user_id, "org_role": org_role, "raw_claims": {}}


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


def _authed(tenant_id: str, user_id: str, org_role: str = "member") -> None:
    app.dependency_overrides[get_claims] = lambda: _claims(tenant_id, user_id, org_role)


@pytest.fixture(autouse=True)
def _clean_intake_questions(owner_conn) -> Iterator[None]:
    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM deal_intake_questions")
    yield
    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM deal_intake_questions")


@pytest.fixture
def seeded_org(owner_conn) -> Iterator[dict[str, Any]]:
    clerk_org_id = f"test-intake-read-org-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (clerk_org_id, "Intake Read Test Org"),
        )
        org_pk = cur.fetchone()[0]

    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk}

    with owner_conn.cursor() as cur:
        for table in ("human_audit_log", "sessions", "users"):
            cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


def _seed_question(
    owner_conn, question_key: str, display_order: int, is_active: bool = True, **fields: Any
) -> str:
    columns = {
        "question_key": question_key,
        "prompt": f"Prompt for {question_key}",
        "input_type": "text",
        "required": True,
        "display_order": display_order,
        "is_active": is_active,
        **fields,
    }
    cols = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    with owner_conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO deal_intake_questions ({cols}) VALUES ({placeholders}) RETURNING id",
            list(columns.values()),
        )
        return str(cur.fetchone()[0])


def test_empty_list_when_no_questions_exist(client, seeded_org):
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get("/intake-questions")

    assert resp.status_code == 200
    assert resp.json() == []


def test_returns_only_active_questions(client, owner_conn, seeded_org):
    """Acceptance criterion: only is_active = true rows."""
    active_id = _seed_question(owner_conn, "company_name", 0, is_active=True)
    _seed_question(owner_conn, "retired_question", 1, is_active=False)
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get("/intake-questions")

    assert resp.status_code == 200
    (question,) = resp.json()
    assert question["id"] == active_id
    assert question["questionKey"] == "company_name"


def test_ordered_by_display_order(client, owner_conn, seeded_org):
    """Acceptance criterion: ordered correctly."""
    third = _seed_question(owner_conn, "third", 2)
    first = _seed_question(owner_conn, "first", 0)
    second = _seed_question(owner_conn, "second", 1)
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get("/intake-questions")

    assert [q["id"] for q in resp.json()] == [first, second, third]


def test_response_shape_has_no_is_active_field(client, owner_conn, seeded_org):
    """Product-side shape is slimmer than the admin one -- is_active would
    always be true here, so it's not exposed at all."""
    _seed_question(owner_conn, "company_name", 0, help_text="Legal entity name")
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get("/intake-questions")

    (question,) = resp.json()
    assert set(question.keys()) == {
        "id",
        "questionKey",
        "prompt",
        "helpText",
        "inputType",
        "required",
        "displayOrder",
    }
    assert question["helpText"] == "Legal entity name"


def test_accessible_to_a_plain_member_not_just_an_admin(client, owner_conn, seeded_org):
    """Acceptance criterion: no auth beyond the normal product
    Depends(get_db) -- any authenticated org user, admin or not."""
    _seed_question(owner_conn, "company_name", 0)
    _authed(seeded_org["clerk_org_id"], "user-1", org_role="member")

    resp = client.get("/intake-questions")

    assert resp.status_code == 200
    assert len(resp.json()) == 1
