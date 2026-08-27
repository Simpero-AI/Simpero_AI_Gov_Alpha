"""Admin CRUD for deal_intake_questions (app/api/admin/intake_questions.py,
P2-02). Same harness as tests/test_admin_portal.py -- duplicated fixtures
rather than shared, per that module's own precedent (fixtures are private
to the file that needs them).

deal_intake_questions is a global reference table (no org_id), so tests
can't rely on org-scoped teardown to isolate rows the way RLS-scoped tables
do -- the autouse cleanup fixture below truncates it around every test.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.core.admin_dependencies as admin_deps_mod
from app.core.dependencies import get_claims
from app.main import app


def _claims(tenant_id: str, user_id: str, org_role: str | None = "admin") -> dict[str, Any]:
    return {"tenant_id": tenant_id, "user_id": user_id, "org_role": org_role, "raw_claims": {}}


class ApiTestClient(TestClient):
    """Prepends /api -- mirrors tests/test_admin_portal.py's idiom."""

    def request(self, method: str, url: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(url, str) and url.startswith("/") and not url.startswith("/api/"):
            url = f"/api{url}"
        return super().request(method, url, *args, **kwargs)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with ApiTestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_claims, None)


def _authed(tenant_id: str, user_id: str, org_role: str | None = "admin") -> None:
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
    clerk_org_id = f"test-intake-org-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (clerk_org_id, "Intake Test Org"),
        )
        org_pk = cur.fetchone()[0]

    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk}

    with owner_conn.cursor() as cur:
        for table in ("human_audit_log", "clerk_admin_users", "users"):
            cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


@pytest.fixture
def platform_org(monkeypatch, owner_conn) -> Iterator[dict[str, Any]]:
    clerk_org_id = f"test-intake-platform-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (clerk_org_id, "Simpero Platform"),
        )
        org_pk = cur.fetchone()[0]
    monkeypatch.setattr(admin_deps_mod.settings, "simpero_platform_org_id", clerk_org_id)

    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk}

    with owner_conn.cursor() as cur:
        for table in ("human_audit_log", "clerk_admin_users", "users"):
            cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


def _seed_admin(owner_conn, org: dict[str, Any], clerk_user_id: str, **fields: Any) -> None:
    columns = {
        "clerk_user_id": clerk_user_id,
        "clerk_org_id": org["clerk_org_id"],
        "org_id": org["org_pk"],
        "admin_type": "client",
        "status": "active",
        **fields,
    }
    cols = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    with owner_conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO clerk_admin_users ({cols}, created_at) VALUES ({placeholders}, now())",
            list(columns.values()),
        )


def _as_platform_admin(owner_conn, platform_org: dict[str, Any]) -> None:
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, platform_org, user_id, admin_type="platform")
    _authed(platform_org["clerk_org_id"], user_id, "admin")


def _create_question(client, **overrides: Any) -> dict[str, Any]:
    payload = {
        "questionKey": f"q_{uuid.uuid4().hex[:8]}",
        "prompt": "What is the company name?",
        "helpText": None,
        "inputType": "text",
        "required": True,
        **overrides,
    }
    resp = client.post("/admin/intake-questions", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- guard: every route denies a non-platform admin token -------------------


@pytest.mark.parametrize(
    ("method", "path_suffix", "body"),
    [
        ("GET", "", None),
        ("POST", "", {"questionKey": "x", "prompt": "x", "inputType": "text"}),
        (
            "PATCH",
            "/{id}",
            {"prompt": "x", "helpText": None, "inputType": "text", "required": False},
        ),
        ("PUT", "/reorder", {"questionIds": []}),
        ("PATCH", "/{id}/activate", None),
        ("PATCH", "/{id}/deactivate", None),
    ],
)
def test_org_admin_denied_on_every_route(client, owner_conn, seeded_org, method, path_suffix, body):
    """P2-02 acceptance criterion: an org admin token gets 403 on every
    route -- require_platform_admin rejects a "client"-typed admin
    regardless of which route it hits."""
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, user_id, admin_type="client")
    _authed(seeded_org["clerk_org_id"], user_id, "admin")

    path = f"/admin/intake-questions{path_suffix.replace('{id}', str(uuid.uuid4()))}"
    resp = client.request(method, path, json=body)

    assert resp.status_code == 403


# --- create -------------------------------------------------------------


def test_create_question_success_and_audit(client, owner_conn, platform_org):
    _as_platform_admin(owner_conn, platform_org)

    question = _create_question(client, questionKey="company_name")

    assert question["questionKey"] == "company_name"
    assert question["isActive"] is True
    assert question["displayOrder"] == 0
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM human_audit_log WHERE event_type = 'admin_intake_question_created'"
        )
        (payload,) = cur.fetchone()
        assert payload["question_key"] == "company_name"


def test_create_question_appends_display_order(client, owner_conn, platform_org):
    _as_platform_admin(owner_conn, platform_org)

    first = _create_question(client)
    second = _create_question(client)

    assert first["displayOrder"] == 0
    assert second["displayOrder"] == 1


def test_create_question_duplicate_active_key_conflict(client, owner_conn, platform_org):
    _as_platform_admin(owner_conn, platform_org)
    _create_question(client, questionKey="company_name")

    resp = client.post(
        "/admin/intake-questions",
        json={
            "questionKey": "company_name",
            "prompt": "Duplicate?",
            "inputType": "text",
        },
    )

    assert resp.status_code == 409


def test_deactivated_keys_key_reuse_on_create(client, owner_conn, platform_org):
    """The whole point of the partial-active index: a new question can
    reuse a deactivated question's key."""
    _as_platform_admin(owner_conn, platform_org)
    first = _create_question(client, questionKey="company_name")
    client.patch(f"/admin/intake-questions/{first['id']}/deactivate")

    resp = client.post(
        "/admin/intake-questions",
        json={"questionKey": "company_name", "prompt": "New copy", "inputType": "text"},
    )

    assert resp.status_code == 201


# --- list -----------------------------------------------------------------


def test_list_questions_includes_inactive(client, owner_conn, platform_org):
    _as_platform_admin(owner_conn, platform_org)
    question = _create_question(client)
    client.patch(f"/admin/intake-questions/{question['id']}/deactivate")

    resp = client.get("/admin/intake-questions")

    assert resp.status_code == 200
    ids = [row["id"] for row in resp.json()]
    assert question["id"] in ids


# --- update -----------------------------------------------------------------


def test_update_question_success_and_audit(client, owner_conn, platform_org):
    _as_platform_admin(owner_conn, platform_org)
    question = _create_question(client)

    resp = client.patch(
        f"/admin/intake-questions/{question['id']}",
        json={
            "prompt": "Updated prompt",
            "helpText": "New help text",
            "inputType": "textarea",
            "required": False,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["prompt"] == "Updated prompt"
    assert body["helpText"] == "New help text"
    assert body["inputType"] == "textarea"
    assert body["required"] is False
    # question_key is not accepted by UpdateIntakeQuestionRequest -- immutable.
    assert body["questionKey"] == question["questionKey"]
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM human_audit_log WHERE event_type = 'admin_intake_question_updated'"
        )
        assert cur.fetchone()[0] == 1


def test_update_question_not_found(client, owner_conn, platform_org):
    _as_platform_admin(owner_conn, platform_org)

    resp = client.patch(
        f"/admin/intake-questions/{uuid.uuid4()}",
        json={"prompt": "x", "helpText": None, "inputType": "text", "required": False},
    )

    assert resp.status_code == 404


# --- activate / deactivate --------------------------------------------------


def test_deactivate_never_hard_deletes(client, owner_conn, platform_org):
    """Acceptance criterion: a deactivated question stops appearing in new
    snapshots (list_active — exercised in P2-03) but the row itself, which
    a past snapshot's text is independent of, is never removed."""
    _as_platform_admin(owner_conn, platform_org)
    question = _create_question(client)

    resp = client.patch(f"/admin/intake-questions/{question['id']}/deactivate")

    assert resp.status_code == 200
    assert resp.json()["isActive"] is False
    with owner_conn.cursor() as cur:
        cur.execute("SELECT is_active FROM deal_intake_questions WHERE id = %s", (question["id"],))
        row = cur.fetchone()
        assert row is not None, "deactivate must not delete the row"
        assert row[0] is False


def test_activate_conflicts_when_key_already_active_elsewhere(client, owner_conn, platform_org):
    _as_platform_admin(owner_conn, platform_org)
    original = _create_question(client, questionKey="company_name")
    client.patch(f"/admin/intake-questions/{original['id']}/deactivate")
    _create_question(client, questionKey="company_name")  # takes over the active key

    resp = client.patch(f"/admin/intake-questions/{original['id']}/activate")

    assert resp.status_code == 409


def test_deactivate_not_found(client, owner_conn, platform_org):
    _as_platform_admin(owner_conn, platform_org)

    resp = client.patch(f"/admin/intake-questions/{uuid.uuid4()}/deactivate")

    assert resp.status_code == 404


# --- reorder ------------------------------------------------------------


def test_reorder_success_and_audit(client, owner_conn, platform_org):
    _as_platform_admin(owner_conn, platform_org)
    first = _create_question(client)
    second = _create_question(client)

    resp = client.put(
        "/admin/intake-questions/reorder",
        json={"questionIds": [second["id"], first["id"]]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert [row["id"] for row in body] == [second["id"], first["id"]]
    assert [row["displayOrder"] for row in body] == [0, 1]
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM human_audit_log WHERE event_type = 'admin_intake_question_reordered'"
        )
        assert cur.fetchone()[0] == 1


def test_reorder_rejects_mismatched_id_set(client, owner_conn, platform_org):
    _as_platform_admin(owner_conn, platform_org)
    first = _create_question(client)

    resp = client.put(
        "/admin/intake-questions/reorder",
        json={"questionIds": [first["id"], str(uuid.uuid4())]},
    )

    assert resp.status_code == 422


def test_reorder_rejects_missing_id(client, owner_conn, platform_org):
    _as_platform_admin(owner_conn, platform_org)
    first = _create_question(client)
    _create_question(client)

    resp = client.put("/admin/intake-questions/reorder", json={"questionIds": [first["id"]]})

    assert resp.status_code == 422


def test_reorder_rejects_duplicate_id(client, owner_conn, platform_org):
    """Review finding: a duplicate id (same set as the current table, wrong
    list) passed a set-only comparison and silently overwrote one row's
    display_order twice instead of 422ing."""
    _as_platform_admin(owner_conn, platform_org)
    first = _create_question(client)
    second = _create_question(client)

    resp = client.put(
        "/admin/intake-questions/reorder",
        json={"questionIds": [first["id"], first["id"]]},
    )

    assert resp.status_code == 422
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT display_order FROM deal_intake_questions WHERE id = %s", (second["id"],)
        )
        assert cur.fetchone()[0] == 1


# --- input_type validation --------------------------------------------------


def test_create_rejects_unsupported_input_type(client, owner_conn, platform_org):
    _as_platform_admin(owner_conn, platform_org)

    resp = client.post(
        "/admin/intake-questions",
        json={"questionKey": "company_name", "prompt": "x", "inputType": "dropdown"},
    )

    assert resp.status_code == 422


def test_update_rejects_unsupported_input_type(client, owner_conn, platform_org):
    _as_platform_admin(owner_conn, platform_org)
    question = _create_question(client)

    resp = client.patch(
        f"/admin/intake-questions/{question['id']}",
        json={"prompt": "x", "helpText": None, "inputType": "dropdown", "required": False},
    )

    assert resp.status_code == 422
