"""GET /deals/{deal_id}/market -- the claims-driven Market tab endpoint.

Locks the wire contract the unit tests (test_market_view.py) don't cover: the
404 vs never-404-empty behavior, the camelCase response keys, and the RLS
tenancy that rests on the get_db dependency. Same TestClient harness as
tests/test_deal_documents_endpoint.py.
"""

import json
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
    clerk_org_id = f"test-market-org-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (clerk_org_id, "Market Endpoint Org"),
        )
        org_pk = cur.fetchone()[0]
    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk}
    with owner_conn.cursor() as cur:
        for table in ("claims", "human_audit_log", "deals", "users"):
            cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


@pytest.fixture
def seeded_deal(owner_conn, seeded_org) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (seeded_org["org_pk"], "Acme"),
        )
        return str(cur.fetchone()[0])


def _seed_sizing_claim(owner_conn, org_pk: int, deal_id: str) -> None:
    value = {"raw": "$5.0B", "normalized": 5_000_000_000, "unit": "USD", "value_type": "currency"}
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO claims (org_id, deal_id, entity, attribute, attribute_raw, value, kind, "
            "page, char_start, char_end, status, verification_method) "
            "VALUES (%s, %s, 'Acme', 'operating_metric', 'Total Addressable Market', %s::jsonb, "
            "'pdf', 3, 100, 120, 'cited', 'exact_span')",
            (org_pk, deal_id, json.dumps(value)),
        )


def test_404_when_deal_missing(client, seeded_org):
    _authed(seeded_org["clerk_org_id"], "user-1")
    resp = client.get(f"/deals/{uuid.uuid4()}/market")
    assert resp.status_code == 404


def test_empty_lists_when_no_market_claims(client, seeded_org, seeded_deal):
    """A claim-less deal is a 200 with empty lists, never a 404 -- the tab renders
    its own 'information not available' state."""
    _authed(seeded_org["clerk_org_id"], "user-1")
    resp = client.get(f"/deals/{seeded_deal}/market")
    assert resp.status_code == 200
    assert resp.json() == {"sizing": [], "marketDefinition": [], "competitivePosition": []}


def test_returns_sizing_with_camelcase_wire_keys(client, owner_conn, seeded_org, seeded_deal):
    _seed_sizing_claim(owner_conn, seeded_org["org_pk"], seeded_deal)
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get(f"/deals/{seeded_deal}/market")

    assert resp.status_code == 200
    body = resp.json()
    # camelCase wire keys (the CamelModel contract the frontend consumes)
    assert set(body.keys()) == {"sizing", "marketDefinition", "competitivePosition"}
    (fact,) = body["sizing"]
    assert set(fact.keys()) == {"label", "value", "citation", "status", "entity"}
    assert fact["label"] == "TAM"
    assert fact["value"] == "$5.00B"
    assert fact["status"] == "cited"


def test_scoped_to_the_caller_org(client, owner_conn, seeded_org, seeded_deal):
    """RLS: a second org can't see this deal at all -- 404, not an empty 200, so
    there's no cross-tenant enumeration oracle."""
    _seed_sizing_claim(owner_conn, seeded_org["org_pk"], seeded_deal)

    other_clerk_org = f"test-market-org-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) "
            "VALUES (%s, %s, now()) RETURNING id",
            (other_clerk_org, "Other Org"),
        )
        other_pk = cur.fetchone()[0]
    try:
        _authed(other_clerk_org, "user-2")
        resp = client.get(f"/deals/{seeded_deal}/market")
        assert resp.status_code == 404
    finally:
        with owner_conn.cursor() as cur:
            for table in ("claims", "human_audit_log", "deals", "users"):
                cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (other_pk,))
            cur.execute("DELETE FROM organisation WHERE id = %s", (other_pk,))
