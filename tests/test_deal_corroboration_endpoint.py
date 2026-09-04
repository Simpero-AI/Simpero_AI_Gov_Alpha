"""GET /deals/{deal_id}/corroboration -- the corroboration display's read side:
every outside-source check run against the deal's claims, each with its
agree/disagree verdict and a link to the external record ("cite the cite").

Same TestClient harness as tests/test_deal_documents_endpoint.py -- duplicated
fixtures rather than shared, per that module's own precedent.
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
    clerk_org_id = f"test-corrob-org-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (clerk_org_id, "Corroboration Endpoint Org"),
        )
        org_pk = cur.fetchone()[0]

    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk}

    with owner_conn.cursor() as cur:
        # corroboration_events + claims reference deals, so drop them before deals.
        for table in ("corroboration_events", "claims", "human_audit_log", "deals", "users"):
            cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


@pytest.fixture
def seeded_deal(owner_conn, seeded_org) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (seeded_org["org_pk"], "Acme Deal"),
        )
        return str(cur.fetchone()[0])


def _seed_claim(
    owner_conn,
    org_pk: int,
    deal_id: str,
    *,
    entity: str = "Acme Corp",
    attribute: str = "revenueLatestUsd",
    value: dict | None = None,
    status: str = "cited",
) -> str:
    value = (
        value
        if value is not None
        else {
            "raw": "$15M",
            "normalized": 15000000,
            "unit": "USD",
            "value_type": "currency",
        }
    )
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO claims (org_id, deal_id, entity, attribute, value, kind, page, "
            "char_start, char_end, status) "
            "VALUES (%s, %s, %s, %s, %s::jsonb, 'pdf', 3, 100, 120, %s) RETURNING id",
            (org_pk, deal_id, entity, attribute, json.dumps(value), status),
        )
        return str(cur.fetchone()[0])


def _seed_event(
    owner_conn,
    org_pk: int,
    claim_id: str,
    *,
    outside_source: str,
    result: dict,
    agrees: bool | None,
    created_at: str | None = None,
) -> str:
    with owner_conn.cursor() as cur:
        if created_at is None:
            cur.execute(
                "INSERT INTO corroboration_events "
                "(org_id, claim_id, outside_source, result, agrees) "
                "VALUES (%s, %s, %s, %s::jsonb, %s) RETURNING id",
                (org_pk, claim_id, outside_source, json.dumps(result), agrees),
            )
        else:
            cur.execute(
                "INSERT INTO corroboration_events "
                "(org_id, claim_id, outside_source, result, agrees, created_at) "
                "VALUES (%s, %s, %s, %s::jsonb, %s, %s) RETURNING id",
                (org_pk, claim_id, outside_source, json.dumps(result), agrees, created_at),
            )
        return str(cur.fetchone()[0])


def test_404_when_deal_missing(client, seeded_org):
    _authed(seeded_org["clerk_org_id"], "user-1")
    resp = client.get(f"/deals/{uuid.uuid4()}/corroboration")
    assert resp.status_code == 404


def test_empty_when_no_events(client, seeded_org, seeded_deal):
    """Never 404 for a deal the corroboration pass hasn't produced events for --
    the tab renders its own empty state off an empty list."""
    _authed(seeded_org["clerk_org_id"], "user-1")
    resp = client.get(f"/deals/{seeded_deal}/corroboration")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "events": [],
        "confirmedCount": 0,
        "conflictingCount": 0,
        "totalCount": 0,
    }


def test_returns_event_with_verdict_and_claim_context(client, owner_conn, seeded_org, seeded_deal):
    claim_id = _seed_claim(owner_conn, seeded_org["org_pk"], seeded_deal, status="cited")
    _seed_event(
        owner_conn,
        seeded_org["org_pk"],
        claim_id,
        outside_source="sec_edgar",
        result={"cik": 320193, "concept": "Revenues", "edgar_value": 15000000},
        agrees=True,
    )
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get(f"/deals/{seeded_deal}/corroboration")

    assert resp.status_code == 200
    body = resp.json()
    assert body["totalCount"] == 1
    assert body["confirmedCount"] == 1
    assert body["conflictingCount"] == 0
    (event,) = body["events"]
    assert event["outsideSource"] == "sec_edgar"
    assert event["agrees"] is True
    assert event["claimId"] == claim_id
    assert event["claimEntity"] == "Acme Corp"
    assert event["claimAttribute"] == "revenueLatestUsd"
    assert event["claimStatus"] == "cited"
    assert event["result"]["cik"] == 320193
    assert "CIK=0000320193" in event["sourceUrl"]


def test_federal_register_event_uses_stored_html_url(client, owner_conn, seeded_org, seeded_deal):
    claim_id = _seed_claim(owner_conn, seeded_org["org_pk"], seeded_deal)
    html_url = "https://www.federalregister.gov/documents/2024/06/01/2024-12345/rule"
    _seed_event(
        owner_conn,
        seeded_org["org_pk"],
        claim_id,
        outside_source="us_federal_register",
        result={"source": "us_federal_register", "document": {"html_url": html_url}},
        agrees=True,
    )
    _authed(seeded_org["clerk_org_id"], "user-1")

    (event,) = client.get(f"/deals/{seeded_deal}/corroboration").json()["events"]
    assert event["sourceUrl"] == html_url


def test_trademark_event_has_no_source_url(client, owner_conn, seeded_org, seeded_deal):
    claim_id = _seed_claim(owner_conn, seeded_org["org_pk"], seeded_deal)
    _seed_event(
        owner_conn,
        seeded_org["org_pk"],
        claim_id,
        outside_source="trademarks_cipo_uspto",
        result={"registry": "uspto", "registration_id": "88123456"},
        agrees=True,
    )
    _authed(seeded_org["clerk_org_id"], "user-1")

    (event,) = client.get(f"/deals/{seeded_deal}/corroboration").json()["events"]
    assert event["sourceUrl"] is None
    # result is passed through verbatim (an open dict), so its keys stay as the
    # source stored them -- snake_case, not camelised like the model's own fields.
    assert event["result"]["registration_id"] == "88123456"


def test_confirmed_and_conflicting_counts(client, owner_conn, seeded_org, seeded_deal):
    confirmed_claim = _seed_claim(owner_conn, seeded_org["org_pk"], seeded_deal, entity="Acme Corp")
    conflicted_claim = _seed_claim(
        owner_conn, seeded_org["org_pk"], seeded_deal, entity="Beta Inc", status="conflicted"
    )
    _seed_event(
        owner_conn,
        seeded_org["org_pk"],
        confirmed_claim,
        outside_source="sec_edgar",
        result={"cik": 111},
        agrees=True,
    )
    _seed_event(
        owner_conn,
        seeded_org["org_pk"],
        conflicted_claim,
        outside_source="sec_edgar",
        result={"cik": 222, "discrepancy_delta": 0.5},
        agrees=False,
    )
    _authed(seeded_org["clerk_org_id"], "user-1")

    body = client.get(f"/deals/{seeded_deal}/corroboration").json()
    assert body["totalCount"] == 2
    assert body["confirmedCount"] == 1
    assert body["conflictingCount"] == 1


def test_dedupes_to_latest_generation_per_claim_and_source(
    client, owner_conn, seeded_org, seeded_deal
):
    """corroboration_events is append-only; a re-analysis appends a fresh
    generation for the same (claim, source). The endpoint keeps only the
    newest."""
    claim_id = _seed_claim(owner_conn, seeded_org["org_pk"], seeded_deal)
    _seed_event(
        owner_conn,
        seeded_org["org_pk"],
        claim_id,
        outside_source="sec_edgar",
        result={"cik": 320193, "generation": "old"},
        agrees=False,
        created_at="2026-01-01T00:00:00+00:00",
    )
    _seed_event(
        owner_conn,
        seeded_org["org_pk"],
        claim_id,
        outside_source="sec_edgar",
        result={"cik": 320193, "generation": "new"},
        agrees=True,
        created_at="2026-02-01T00:00:00+00:00",
    )
    _authed(seeded_org["clerk_org_id"], "user-1")

    body = client.get(f"/deals/{seeded_deal}/corroboration").json()
    assert body["totalCount"] == 1
    (event,) = body["events"]
    assert event["result"]["generation"] == "new"
    assert event["agrees"] is True


def test_scoped_to_the_caller_org(client, owner_conn, seeded_org, seeded_deal):
    """RLS: a second org can't see this deal at all -- 404, not an empty list,
    same idiom as the other deal-scoped reads."""
    claim_id = _seed_claim(owner_conn, seeded_org["org_pk"], seeded_deal)
    _seed_event(
        owner_conn,
        seeded_org["org_pk"],
        claim_id,
        outside_source="sec_edgar",
        result={"cik": 320193},
        agrees=True,
    )

    other_clerk_org = f"test-corrob-org-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) "
            "VALUES (%s, %s, now()) RETURNING id",
            (other_clerk_org, "Other Org"),
        )
        other_pk = cur.fetchone()[0]
    try:
        _authed(other_clerk_org, "user-2")
        resp = client.get(f"/deals/{seeded_deal}/corroboration")
        assert resp.status_code == 404
    finally:
        with owner_conn.cursor() as cur:
            for table in ("corroboration_events", "claims", "human_audit_log", "deals", "users"):
                cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (other_pk,))
            cur.execute("DELETE FROM organisation WHERE id = %s", (other_pk,))
