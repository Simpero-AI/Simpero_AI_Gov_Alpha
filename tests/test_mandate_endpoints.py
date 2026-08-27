"""GET/PUT /api/mandate and GET /api/mandate-categories.

These endpoints had no test coverage at all. They get some here because
SIM-414 makes screening a second reader of the blob PUT /mandate writes, so
the write side's exact output shape is now load-bearing for gs_07/gs_08 --
this file pins it independently of anything screening does with it.

Same harness as tests/test_phase1_endpoints.py (real app through TestClient,
only get_claims overridden, get_db running for real), duplicated rather than
shared: that module's fixtures are private to it and `seeded_org` is already
duplicated across the suite.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.dependencies import get_claims
from app.main import app


def _claims(tenant_id: str, user_id: str) -> dict[str, Any]:
    return {"tenant_id": tenant_id, "user_id": user_id, "org_role": "admin", "raw_claims": {}}


class ApiTestClient(TestClient):
    """Prepends /api — every route is mounted there (app/main.py)."""

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
            (clerk_org_id, "Mandate Test Org"),
        )
        org_pk = cur.fetchone()[0]

    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk}

    with owner_conn.cursor() as cur:
        # `mandates` before `users`: mandates.user_id is a plain NOT NULL FK
        # with no ON DELETE, so the other order is a ForeignKeyViolation.
        for table in ("mandates", "human_audit_log", "sessions", "users"):
            cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


def _audit_payloads(owner_conn, org_pk: int, event_type: str) -> list[Any]:
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM human_audit_log WHERE org_id = %s AND event_type = %s "
            "ORDER BY created_at",
            (org_pk, event_type),
        )
        return [r[0] for r in cur.fetchall()]


GEOGRAPHIES = [
    {
        "category": "Geographies",
        "slug": "geographies",
        "options": [
            {"option": "Canada", "option_id": str(uuid.uuid4())},
            {"option": "United States", "option_id": str(uuid.uuid4())},
        ],
    }
]


def test_get_mandate_is_null_when_unset(client, seeded_org):
    """Null, never 404 — same idiom as GET /investment-profile."""
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get("/mandate")

    assert resp.status_code == 200
    assert resp.json() is None


def test_put_mandate_round_trips_the_blob_verbatim(client, seeded_org):
    """The blob is stored and returned as-is: snake_case keys inside
    (option_id, sub_options) survive, because this payload is never passed
    through CamelModel. Screening's transformer depends on exactly that."""
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.put("/mandate", json={"mandate": GEOGRAPHIES})

    assert resp.status_code == 200
    assert resp.json()["mandate"] == GEOGRAPHIES
    assert resp.json()["updatedAt"] is not None
    assert client.get("/mandate").json()["mandate"] == GEOGRAPHIES


def test_put_mandate_replaces_rather_than_merging(client, owner_conn, seeded_org):
    """One row per org, always a full replace — never a partial patch."""
    _authed(seeded_org["clerk_org_id"], "user-1")
    client.put("/mandate", json={"mandate": GEOGRAPHIES})

    replacement = [{"category": "Target Sectors", "slug": "target_sectors", "options": []}]
    resp = client.put("/mandate", json={"mandate": replacement})

    assert resp.status_code == 200
    assert resp.json()["mandate"] == replacement
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM mandates WHERE org_id = %s", (seeded_org["org_pk"],))
        assert cur.fetchone()[0] == 1


def test_put_mandate_writes_an_audit_row(client, owner_conn, seeded_org):
    _authed(seeded_org["clerk_org_id"], "user-1")

    client.put("/mandate", json={"mandate": GEOGRAPHIES})

    payloads = _audit_payloads(owner_conn, seeded_org["org_pk"], "mandate_saved")
    assert len(payloads) == 1
    (diff,) = payloads
    assert [entry["category"] for entry in diff] == ["Geographies"]
    assert sorted(diff[0]["added"]) == ["Canada", "United States"]


def test_put_mandate_with_no_change_records_an_empty_diff(client, owner_conn, seeded_org):
    """Save clicked with nothing edited: the row is still written, but the
    diff is empty rather than a full re-add of every option."""
    _authed(seeded_org["clerk_org_id"], "user-1")
    client.put("/mandate", json={"mandate": GEOGRAPHIES})

    client.put("/mandate", json={"mandate": GEOGRAPHIES})

    payloads = _audit_payloads(owner_conn, seeded_org["org_pk"], "mandate_saved")
    assert len(payloads) == 2
    assert payloads[1] == []


def test_put_mandate_accepts_the_check_size_entry_shape(client, seeded_org):
    """The one entry with no `options` key at all. Screening has to skip it
    without tripping, so the write side has to keep accepting it."""
    _authed(seeded_org["clerk_org_id"], "user-1")
    blob = [
        {
            "category": "Check Size Range",
            "slug": "check_size_range",
            "min": 5000000,
            "max": 10000000,
        }
    ]

    resp = client.put("/mandate", json={"mandate": blob})

    assert resp.status_code == 200
    assert resp.json()["mandate"] == blob


def test_mandate_is_scoped_to_the_saving_org(client, owner_conn, seeded_org):
    """RLS: a second org sees its own mandate, not this one's."""
    _authed(seeded_org["clerk_org_id"], "user-1")
    client.put("/mandate", json={"mandate": GEOGRAPHIES})

    other_clerk_org = f"test-tenant-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) "
            "VALUES (%s, %s, now()) RETURNING id",
            (other_clerk_org, "Other Org"),
        )
        other_pk = cur.fetchone()[0]
    try:
        _authed(other_clerk_org, "user-2")
        assert client.get("/mandate").json() is None
    finally:
        with owner_conn.cursor() as cur:
            for table in ("mandates", "human_audit_log", "sessions", "users"):
                cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (other_pk,))
            cur.execute("DELETE FROM organisation WHERE id = %s", (other_pk,))
