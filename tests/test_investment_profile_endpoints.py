"""GET and POST/PUT /api/investment-profile.

The write route is investmentProfile.upsert — the mandate/firm-profile
Scorecard's save path. It had no test coverage because no write route existed:
the tRPC mutation the UI used to call was removed with the FastAPI migration
and never ported, so Firm Profile / Scoring Framework saves 404'd (as HTML at
the ingress, not JSON — Shane QA category 3). This pins the ported write side:
its partial-update contract (each tab saves its own column without clobbering
the other), its wire shape (camelCase), and its RLS org-scoping.

Same harness as tests/test_mandate_endpoints.py (real app through TestClient,
only get_claims overridden, get_db running for real), duplicated rather than
shared — that repo precedent keeps small fixtures local to the file.
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
            (clerk_org_id, "Investment Profile Test Org"),
        )
        org_pk = cur.fetchone()[0]

    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk}

    with owner_conn.cursor() as cur:
        for table in ("investment_profiles", "human_audit_log", "sessions", "users"):
            cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


def test_get_is_null_when_unset(client, seeded_org):
    """Null, never 404 — the empty-state contract the UI relies on."""
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get("/investment-profile")

    assert resp.status_code == 200
    assert resp.json() is None


def test_put_creates_then_get_returns_it(client, seeded_org):
    """Firm Profile's slice — firm_name + the mandate blob — round-trips
    camelCase in and out, and the created row is visible to a later GET."""
    _authed(seeded_org["clerk_org_id"], "user-1")
    mandate = {"aum": "$700M+", "firmTypeFreeText": "Growth Equity", "hqLocation": "Vancouver"}

    resp = client.put("/investment-profile", json={"firmName": "Vistara", "mandate": mandate})

    assert resp.status_code == 200
    body = resp.json()
    assert body["firmName"] == "Vistara"
    assert body["mandate"] == mandate
    assert body["weights"] == {}
    assert body["updatedAt"] is not None

    got = client.get("/investment-profile").json()
    assert got["firmName"] == "Vistara"
    assert got["mandate"] == mandate


def test_post_also_upserts(client, seeded_org):
    """POST and PUT hit the same handler — the retired tRPC client POSTed, so
    the ported route accepts both."""
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.post("/investment-profile", json={"firmName": "Acme"})

    assert resp.status_code == 200
    assert resp.json()["firmName"] == "Acme"


def test_saving_one_slice_leaves_the_other_column_untouched(client, seeded_org):
    """The partial-update contract: Firm Profile saves firm_name + mandate,
    Scoring Framework saves weights, and neither wipes the other's column even
    though both go through this one endpoint."""
    _authed(seeded_org["clerk_org_id"], "user-1")
    mandate = {"aum": "$700M+"}
    weights = {"framework": {"categories": [{"id": "team", "name": "Team", "weight": 100}]}}

    client.put("/investment-profile", json={"firmName": "Vistara", "mandate": mandate})
    # Scoring Framework saves weights only — no firmName / mandate in the body.
    resp = client.put("/investment-profile", json={"weights": weights})

    assert resp.status_code == 200
    body = resp.json()
    assert body["weights"] == weights
    # firm_name + mandate from the first save are still there, not nulled.
    assert body["firmName"] == "Vistara"
    assert body["mandate"] == mandate


def test_explicit_null_for_an_untouched_field_does_not_clobber(client, seeded_org):
    """A client that serializes an untouched field as explicit null (rather than
    omitting it) must not wipe the other tab's column — the route drops
    None-valued keys before the upsert."""
    _authed(seeded_org["clerk_org_id"], "user-1")
    client.put("/investment-profile", json={"firmName": "Vistara", "mandate": {"aum": "$700M+"}})

    weights = {"framework": {"categories": []}}
    resp = client.put("/investment-profile", json={"weights": weights, "firmName": None})

    assert resp.status_code == 200
    body = resp.json()
    assert body["weights"] == weights
    assert body["firmName"] == "Vistara"  # not nulled out


def test_put_replaces_the_targeted_jsonb_column_wholesale(client, seeded_org):
    """A second Firm Profile save of `mandate` is a full replace of that
    column, not a deep merge — the caller merges client-side before sending."""
    _authed(seeded_org["clerk_org_id"], "user-1")
    client.put("/investment-profile", json={"mandate": {"aum": "$100M", "hqLocation": "NYC"}})

    resp = client.put("/investment-profile", json={"mandate": {"aum": "$500M"}})

    assert resp.status_code == 200
    assert resp.json()["mandate"] == {"aum": "$500M"}


def test_scoped_to_the_saving_org(client, owner_conn, seeded_org):
    """RLS: a second org neither sees nor overwrites this org's profile."""
    _authed(seeded_org["clerk_org_id"], "user-1")
    client.put("/investment-profile", json={"firmName": "Org One"})

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
        assert client.get("/investment-profile").json() is None
        client.put("/investment-profile", json={"firmName": "Org Two"})

        _authed(seeded_org["clerk_org_id"], "user-1")
        assert client.get("/investment-profile").json()["firmName"] == "Org One"
    finally:
        with owner_conn.cursor() as cur:
            for table in ("investment_profiles", "human_audit_log", "sessions", "users"):
                cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (other_pk,))
            cur.execute("DELETE FROM organisation WHERE id = %s", (other_pk,))
