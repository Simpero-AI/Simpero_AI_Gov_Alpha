"""POST/GET /deals/{id}/entity-resolution (SIM-262).

Hits the real app through TestClient with only `get_claims` overridden, same
harness as tests/test_phase1_endpoints.py -- `get_db` runs for real, so RLS,
SET LOCAL and JIT provisioning are all exercised. The resolver itself is
stubbed: this file is about the endpoint contract, not SEC's data, and
tests/test_entity_resolution_edgar.py already pins the adapter's judgment.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api.deals as deals_api
from app.core.dependencies import get_claims
from app.main import app
from app.services.entity_resolution.types import (
    EntityResolutionError,
    FormerName,
    Resolution,
)


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


@pytest.fixture
def seeded_org(owner_conn) -> Iterator[dict[str, Any]]:
    clerk_org_id = f"test-tenant-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (clerk_org_id, "Entity Resolution Org"),
        )
        org_pk = cur.fetchone()[0]

    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk}

    with owner_conn.cursor() as cur:
        for table in ("entity_resolution", "human_audit_log", "deals", "users"):
            cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


def _seed_deal(owner_conn, org_pk: int, name: str = "Meta Platforms, Inc.") -> str:
    with owner_conn.cursor() as cur:
        cur.execute("INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id", (org_pk, name))
        return str(cur.fetchone()[0])


class _StubResolver:
    """Records what it was asked, so the tests can assert the endpoint passes
    the DEAL's name rather than anything it invented."""

    def __init__(self, resolution: Resolution | Exception) -> None:
        self._resolution = resolution
        self.asked: list[str] = []

    @property
    def source(self) -> str:
        return "sec_edgar"

    async def resolve(self, name: str) -> Resolution:
        self.asked.append(name)
        if isinstance(self._resolution, Exception):
            raise self._resolution
        return self._resolution


def _stub(monkeypatch, resolution: Resolution | Exception) -> _StubResolver:
    resolver = _StubResolver(resolution)
    monkeypatch.setattr(deals_api, "get_resolver", lambda: resolver)
    return resolver


def _resolved(name: str = "Meta Platforms, Inc.") -> Resolution:
    return Resolution(
        status="resolved",
        source="sec_edgar",
        query_name=name,
        registry_id="0001326801",
        legal_name="Meta Platforms, Inc.",
        former_names=(
            FormerName(name="Facebook Inc", from_date="2005-05-06", to_date="2021-10-27"),
        ),
        matched_on="former_name",
        evidence={"normalized_query": "META PLATFORMS", "candidates": 1},
    )


def _not_found(name: str = "Private Co") -> Resolution:
    return Resolution(
        status="not_found",
        source="sec_edgar",
        query_name=name,
        reason="No SEC filer matches this name.",
        evidence={"candidates": 0},
    )


# --------------------------------------------------------------------------
# POST
# --------------------------------------------------------------------------


def test_post_resolves_and_returns_the_row(client, owner_conn, seeded_org, monkeypatch) -> None:
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"])
    resolver = _stub(monkeypatch, _resolved())
    app.dependency_overrides[get_claims] = lambda: _claims(seeded_org["clerk_org_id"], "user_1")

    response = client.post(f"/deals/{deal_id}/entity-resolution")

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "resolved"
    assert body["registryId"] == "0001326801"
    assert body["matchedOn"] == "former_name"
    assert body["dealId"] == deal_id
    # camelCase on the wire, and `from`/`to` keep EDGAR's own spelling rather
    # than picking up a trailing underscore.
    assert body["formerNames"] == [
        {"name": "Facebook Inc", "from": "2005-05-06", "to": "2021-10-27"}
    ]
    # The resolver is asked for the DEAL's name, not anything reconstructed.
    assert resolver.asked == ["Meta Platforms, Inc."]


def test_post_records_not_found_as_a_201_not_a_404(
    client, owner_conn, seeded_org, monkeypatch
) -> None:
    """Absence is not contradiction. "We looked and SEC has no filer" is a
    real answer worth storing, and 404 would make it indistinguishable from
    "we never looked"."""
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"], name="Private Co")
    _stub(monkeypatch, _not_found())
    app.dependency_overrides[get_claims] = lambda: _claims(seeded_org["clerk_org_id"], "user_1")

    response = client.post(f"/deals/{deal_id}/entity-resolution")

    assert response.status_code == 201
    assert response.json()["status"] == "not_found"
    assert response.json()["registryId"] is None


def test_post_appends_rather_than_replacing(client, owner_conn, seeded_org, monkeypatch) -> None:
    """A company that was not_found before it filed, then resolved after --
    both rows survive as the record of how the answer changed."""
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"])
    app.dependency_overrides[get_claims] = lambda: _claims(seeded_org["clerk_org_id"], "user_1")

    _stub(monkeypatch, _not_found("Meta Platforms, Inc."))
    first = client.post(f"/deals/{deal_id}/entity-resolution")
    _stub(monkeypatch, _resolved())
    second = client.post(f"/deals/{deal_id}/entity-resolution")

    assert first.json()["id"] != second.json()["id"]
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM entity_resolution WHERE deal_id = %s", (deal_id,))
        assert cur.fetchone()[0] == 2


def test_post_writes_an_audit_event(client, owner_conn, seeded_org, monkeypatch) -> None:
    """The append-only trail carries the full resolution independently of the
    entity_resolution table."""
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"])
    _stub(monkeypatch, _resolved())
    app.dependency_overrides[get_claims] = lambda: _claims(seeded_org["clerk_org_id"], "user_1")

    client.post(f"/deals/{deal_id}/entity-resolution")

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM human_audit_log "
            "WHERE deal_id = %s AND event_type = 'deal_entity_resolved'",
            (deal_id,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0]["registry_id"] == "0001326801"


def test_post_on_a_registry_outage_is_502_and_stores_nothing(
    client, owner_conn, seeded_org, monkeypatch
) -> None:
    """An unreachable SEC is not a finding about the company. Nothing is
    persisted, so a retry after recovery is clean rather than leaving an
    "error" row that later reads like evidence."""
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"])
    _stub(monkeypatch, EntityResolutionError("SEC request failed: timeout"))
    app.dependency_overrides[get_claims] = lambda: _claims(seeded_org["clerk_org_id"], "user_1")

    response = client.post(f"/deals/{deal_id}/entity-resolution")

    assert response.status_code == 502
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM entity_resolution WHERE deal_id = %s", (deal_id,))
        assert cur.fetchone()[0] == 0


def test_post_on_an_unknown_deal_is_404(client, seeded_org, monkeypatch) -> None:
    resolver = _stub(monkeypatch, _resolved())
    app.dependency_overrides[get_claims] = lambda: _claims(seeded_org["clerk_org_id"], "user_1")

    response = client.post(f"/deals/{uuid.uuid4()}/entity-resolution")

    assert response.status_code == 404
    # Never reached SEC for a deal that does not exist.
    assert resolver.asked == []


# --------------------------------------------------------------------------
# GET
# --------------------------------------------------------------------------


def test_get_returns_the_latest_resolution(client, owner_conn, seeded_org, monkeypatch) -> None:
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"])
    app.dependency_overrides[get_claims] = lambda: _claims(seeded_org["clerk_org_id"], "user_1")

    _stub(monkeypatch, _not_found("Meta Platforms, Inc."))
    client.post(f"/deals/{deal_id}/entity-resolution")
    _stub(monkeypatch, _resolved())
    client.post(f"/deals/{deal_id}/entity-resolution")

    response = client.get(f"/deals/{deal_id}/entity-resolution")

    assert response.status_code == 200
    assert response.json()["status"] == "resolved"


def test_get_on_a_never_resolved_deal_is_404_with_its_own_detail(
    client, owner_conn, seeded_org
) -> None:
    """Distinct from the unknown-deal 404 -- "we never looked" and "no such
    deal" are different problems for whoever reads this."""
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"])
    app.dependency_overrides[get_claims] = lambda: _claims(seeded_org["clerk_org_id"], "user_1")

    response = client.get(f"/deals/{deal_id}/entity-resolution")

    assert response.status_code == 404
    assert "not been resolved" in response.json()["detail"]


def test_get_on_an_unknown_deal_is_404_deal_not_found(client, seeded_org) -> None:
    app.dependency_overrides[get_claims] = lambda: _claims(seeded_org["clerk_org_id"], "user_1")

    response = client.get(f"/deals/{uuid.uuid4()}/entity-resolution")

    assert response.status_code == 404
    assert response.json()["detail"] == "Deal not found"


def test_a_resolved_not_found_still_returns_200(
    client, owner_conn, seeded_org, monkeypatch
) -> None:
    """The distinction the GET docstring turns on: a deal we DID check and
    found no filer for is a 200 carrying not_found, never a 404."""
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"], name="Private Co")
    _stub(monkeypatch, _not_found())
    app.dependency_overrides[get_claims] = lambda: _claims(seeded_org["clerk_org_id"], "user_1")
    client.post(f"/deals/{deal_id}/entity-resolution")

    response = client.get(f"/deals/{deal_id}/entity-resolution")

    assert response.status_code == 200
    assert response.json()["status"] == "not_found"


# --------------------------------------------------------------------------
# Tenant isolation.
# --------------------------------------------------------------------------


def test_another_orgs_deal_is_a_404_not_its_data(
    client, owner_conn, seeded_org, monkeypatch
) -> None:
    """RLS makes the other tenant's deal invisible, so this reads as "no such
    deal" -- the resolution is never disclosed and never overwritten."""
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"])
    _stub(monkeypatch, _resolved())
    app.dependency_overrides[get_claims] = lambda: _claims(seeded_org["clerk_org_id"], "user_1")
    client.post(f"/deals/{deal_id}/entity-resolution")

    other_clerk_org = f"other-tenant-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) "
            "VALUES (%s, %s, now()) RETURNING id",
            (other_clerk_org, "Other Org"),
        )
        other_org_pk = cur.fetchone()[0]

    app.dependency_overrides[get_claims] = lambda: _claims(other_clerk_org, "user_2")
    get_response = client.get(f"/deals/{deal_id}/entity-resolution")
    post_response = client.post(f"/deals/{deal_id}/entity-resolution")

    assert get_response.status_code == 404
    assert post_response.status_code == 404

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE org_id = %s", (other_org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (other_org_pk,))
