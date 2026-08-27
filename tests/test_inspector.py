"""Pipeline Inspector endpoint (app/api/inspector.py).

Runs the real handler through the app with a Clerk-claims override (so get_db's
SET LOCAL app.org_id runs for real) and seeds via owner_conn (RLS bypass). The
point: the page is a self-contained HTML document, embeds the deal's claims as a
JSON island, and RLS keeps another org's claims out.
"""

import json
import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.dependencies import get_claims
from app.main import app


class ApiTestClient(TestClient):
    def request(self, method: str, url: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(url, str) and url.startswith("/") and not url.startswith("/api/"):
            url = f"/api{url}"
        return super().request(method, url, *args, **kwargs)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with ApiTestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_claims, None)


def _authed(tenant_id: str) -> None:
    app.dependency_overrides[get_claims] = lambda: {
        "tenant_id": tenant_id,
        "user_id": "user-1",
        "org_role": "admin",
        "raw_claims": {},
    }


@pytest.fixture
def seeded_org(owner_conn) -> Iterator[dict[str, Any]]:
    clerk_org_id = f"test-tenant-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (clerk_org_id, "Inspector Org"),
        )
        org_pk = cur.fetchone()[0]
    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk}
    with owner_conn.cursor() as cur:
        # users/sessions are JIT-created by the auth flow on first request;
        # delete them (and everything FK'd to the org) before the org row.
        for table in (
            "edges",
            "claims",
            "data_source",
            "deals",
            "sessions",
            "users",
            "organisation",
        ):
            col = "id" if table == "organisation" else "org_id"
            cur.execute(f"DELETE FROM {table} WHERE {col} = %s", (org_pk,))


def _seed_deal(owner_conn, org_pk: int, name: str = "Inspector Deal") -> str:
    with owner_conn.cursor() as cur:
        cur.execute("INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id", (org_pk, name))
        return str(cur.fetchone()[0])


def _seed_claim(owner_conn, org_pk: int, deal_id: str, **f) -> str:
    cols = {
        "org_id": org_pk,
        "deal_id": deal_id,
        "entity": "Acme Corp",
        "attribute": "revenue",
        "value": json.dumps(
            {
                "raw": "$15,295K",
                "normalized": 15295000,
                "unit": "USD",
                "value_type": "currency",
                "scale_multiplier": 1000,
                "scale_source": "column_header",
            }
        ),
        "kind": "pdf",
        "page": 12,
        "char_start": 100,
        "char_end": 110,
        "status": "verified",
        "verification_method": "exact_span",
        "claim_type": "numerical",
    }
    cols.update(f)
    names = ", ".join(cols)
    placeholders = ", ".join("%s::jsonb" if k == "value" else "%s" for k in cols)
    with owner_conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO claims ({names}) VALUES ({placeholders}) RETURNING id",
            list(cols.values()),
        )
        return str(cur.fetchone()[0])


def _data_island(html: str) -> dict:
    m = re.search(
        r'<script id="pipeline-data" type="application/json">(.*?)</script>', html, re.DOTALL
    )
    assert m, "data island not found in the page"
    return json.loads(m.group(1).replace("<\\/", "</"))


def test_renders_a_self_contained_page_with_the_deals_claims(client, owner_conn, seeded_org):
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"])
    _seed_claim(owner_conn, seeded_org["org_pk"], deal_id, status="verified")
    _seed_claim(
        owner_conn,
        seeded_org["org_pk"],
        deal_id,
        attribute="headcount",
        status="conflicted",
        value=json.dumps(
            {
                "raw": "48",
                "normalized": 48,
                "unit": None,
                "value_type": "count",
                "scale_multiplier": None,
                "scale_source": "not_applicable",
            }
        ),
    )
    _authed(seeded_org["clerk_org_id"])

    resp = client.get(f"/inspector/{deal_id}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "<!doctype html>" in resp.text.lower()

    data = _data_island(resp.text)
    assert data["deal"]["name"] == "Inspector Deal"
    statuses = sorted(c["status"] for c in data["claims"])
    assert statuses == ["conflicted", "verified"]
    revenue = next(c for c in data["claims"] if c["attribute"] == "revenue")
    assert revenue["value"]["normalized"] == 15295000
    assert revenue["location"] == {
        "kind": "pdf",
        "page": 12,
        "char_start": 100,
        "char_end": 110,
        "sheet": None,
        "cell_ref": None,
        "paragraph": None,
    }


def test_same_fact_and_contradicts_are_summarised_per_claim(client, owner_conn, seeded_org):
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"])
    a = _seed_claim(owner_conn, seeded_org["org_pk"], deal_id, page=1, char_start=1, char_end=5)
    b = _seed_claim(owner_conn, seeded_org["org_pk"], deal_id, page=5, char_start=1, char_end=5)
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO edges (org_id, from_claim_id, to_claim_id, type, basis, created_by) "
            "VALUES (%s, %s, %s, 'same_fact', 'same figure', 'reconciliation')",
            (seeded_org["org_pk"], a, b),
        )
    _authed(seeded_org["clerk_org_id"])

    data = _data_island(client.get(f"/inspector/{deal_id}").text)
    by_id = {c["id"]: c for c in data["claims"]}
    assert by_id[a]["same_fact_count"] == 1
    assert by_id[b]["same_fact_count"] == 1
    assert by_id[a]["contradicts"] is False


def test_rls_hides_another_orgs_claims(client, owner_conn, seeded_org):
    """The page must only ever show the caller's org. A second org's deal/claim
    must not leak into a request authed as the first org."""
    my_deal = _seed_deal(owner_conn, seeded_org["org_pk"])
    _seed_claim(owner_conn, seeded_org["org_pk"], my_deal, entity="Mine")

    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (f"other-{uuid.uuid4().hex[:8]}", "Other Org"),
        )
        other_pk = cur.fetchone()[0]
    other_deal = _seed_deal(owner_conn, other_pk, name="Other Deal")
    _seed_claim(owner_conn, other_pk, other_deal, entity="Theirs")

    _authed(seeded_org["clerk_org_id"])
    # The other org's deal is invisible under this org's RLS scope -> 404.
    assert client.get(f"/inspector/{other_deal}").status_code == 404

    data = _data_island(client.get(f"/inspector/{my_deal}").text)
    assert [c["entity"] for c in data["claims"]] == ["Mine"]

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM claims WHERE org_id = %s", (other_pk,))
        cur.execute("DELETE FROM deals WHERE org_id = %s", (other_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (other_pk,))


def test_unknown_deal_is_404(client, owner_conn, seeded_org):
    _authed(seeded_org["clerk_org_id"])
    assert client.get(f"/inspector/{uuid.uuid4()}").status_code == 404


def test_dashboard_structure_is_carried_into_the_data_island(client, owner_conn, seeded_org):
    """The deal's organizing structure reaches the page so it renders subjects
    and metric order from it; a deal without one carries null (the page then
    falls back to deterministic frequency grouping)."""
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"])
    _seed_claim(owner_conn, seeded_org["org_pk"], deal_id)
    structure = {
        "subjects": [{"name": "Consolidated", "kind": "consolidated", "entities": ["Acme Corp"]}],
        "metric_order": ["revenue", "ebitda"],
    }
    with owner_conn.cursor() as cur:
        cur.execute(
            "UPDATE deals SET dashboard_structure = %s::jsonb WHERE id = %s",
            (json.dumps(structure), deal_id),
        )
    _authed(seeded_org["clerk_org_id"])

    data = _data_island(client.get(f"/inspector/{deal_id}").text)
    assert data["dashboard_structure"] == structure

    bare = _seed_deal(owner_conn, seeded_org["org_pk"], name="No Structure")
    _seed_claim(owner_conn, seeded_org["org_pk"], bare)
    data = _data_island(client.get(f"/inspector/{bare}").text)
    assert data["dashboard_structure"] is None
