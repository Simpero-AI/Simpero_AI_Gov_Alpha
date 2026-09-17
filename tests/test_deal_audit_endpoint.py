"""GET /deals/{deal_id}/audit -- the deal-scoped audit trail behind the Logs
drawer's "Audit Trail" tab, migrated off the retired tRPC logs.auditTrail (which
keyed on a legacy numeric deal id -> Number(uuid) === NaN, so every deal failed
to load). Same harness as tests/test_deal_documents_endpoint.py -- duplicated
fixtures rather than shared, per that module's precedent.
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
    clerk_org_id = f"test-audit-org-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (clerk_org_id, "Audit Test Org"),
        )
        org_pk = cur.fetchone()[0]

    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk}

    with owner_conn.cursor() as cur:
        for table in ("human_audit_log", "data_source", "deals", "users"):
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


def _seed_audit(
    owner_conn,
    org_pk: int,
    deal_id: str | None,
    event_type: str,
    *,
    payload: dict[str, Any] | None = None,
    actor_email: str | None = None,
    row_id: str | None = None,
    created_at: str | None = None,
) -> str:
    columns = ["org_id", "event_type", "deal_id"]
    placeholders = ["%s", "%s", "%s"]
    values: list[Any] = [org_pk, event_type, deal_id]
    if payload is not None:
        columns.append("payload")
        placeholders.append("%s::jsonb")
        values.append(json.dumps(payload))
    if actor_email is not None:
        columns.append("actor_email")
        placeholders.append("%s")
        values.append(actor_email)
    if row_id is not None:
        columns.append("id")
        placeholders.append("%s")
        values.append(row_id)
    if created_at is not None:
        columns.append("created_at")
        placeholders.append("%s")
        values.append(created_at)
    with owner_conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO human_audit_log ({', '.join(columns)}) "
            f"VALUES ({', '.join(placeholders)}) RETURNING id",
            values,
        )
        return str(cur.fetchone()[0])


def test_404_when_deal_missing(client, seeded_org):
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get(f"/deals/{uuid.uuid4()}/audit")

    assert resp.status_code == 404


def test_empty_list_when_no_audit_rows(client, seeded_org, seeded_deal):
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get(f"/deals/{seeded_deal}/audit")

    assert resp.status_code == 200
    assert resp.json() == []


def test_row_shape_and_field_mapping(client, owner_conn, seeded_org, seeded_deal):
    """event_type -> action, payload passthrough, job_id always null. Pins the
    exact camelCased response shape so a renamed field is a test failure."""
    _seed_audit(
        owner_conn,
        seeded_org["org_pk"],
        seeded_deal,
        "analysis_screening_completed",
        payload={"status": "successful", "recommendation": "green"},
        actor_email="Internal System",
    )
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get(f"/deals/{seeded_deal}/audit")

    assert resp.status_code == 200
    (row,) = resp.json()
    assert set(row.keys()) == {
        "id",
        "createdAt",
        "action",
        "sessionId",
        "jobId",
        "actorEmail",
        "payload",
    }
    assert row["action"] == "analysis_screening_completed"
    assert row["actorEmail"] == "Internal System"
    assert row["jobId"] is None
    assert row["payload"] == {"status": "successful", "recommendation": "green"}
    assert row["createdAt"] is not None


def test_newest_first(client, owner_conn, seeded_org, seeded_deal):
    older = _seed_audit(
        owner_conn,
        seeded_org["org_pk"],
        seeded_deal,
        "analysis_started",
        created_at="2026-08-01T00:00:00+00:00",
    )
    newer = _seed_audit(
        owner_conn,
        seeded_org["org_pk"],
        seeded_deal,
        "analysis_screening_completed",
        created_at="2026-08-02T00:00:00+00:00",
    )
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get(f"/deals/{seeded_deal}/audit")

    assert [row["id"] for row in resp.json()] == [newer, older]


def test_only_this_deals_rows(client, owner_conn, seeded_org, seeded_deal):
    """A second deal in the SAME org has its own audit rows; this endpoint is
    deal-scoped, so they must not leak into another deal's trail."""
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (seeded_org["org_pk"], "Other Deal"),
        )
        other_deal = str(cur.fetchone()[0])
    mine = _seed_audit(owner_conn, seeded_org["org_pk"], seeded_deal, "analysis_started")
    _seed_audit(owner_conn, seeded_org["org_pk"], other_deal, "analysis_started")
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get(f"/deals/{seeded_deal}/audit")

    assert [row["id"] for row in resp.json()] == [mine]


def test_deterministic_order_for_identical_created_at(client, owner_conn, seeded_org, seeded_deal):
    """created_at is now() (transaction-start) for chained rows, so several can
    share a timestamp; the id DESC tiebreak keeps ordering stable. Newest-first
    + id DESC => the higher id sorts before the lower one."""
    same = "2026-08-01T00:00:00+00:00"
    lower = "00000000-0000-0000-0000-000000000001"
    higher = "00000000-0000-0000-0000-000000000002"
    _seed_audit(owner_conn, seeded_org["org_pk"], seeded_deal, "a", row_id=lower, created_at=same)
    _seed_audit(owner_conn, seeded_org["org_pk"], seeded_deal, "b", row_id=higher, created_at=same)
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get(f"/deals/{seeded_deal}/audit")

    assert [row["id"] for row in resp.json()] == [higher, lower]


def test_scoped_to_the_caller_org(client, owner_conn, seeded_org, seeded_deal):
    """RLS: a second org can't see this deal at all -- 404, not an empty list,
    same idiom as GET /deals/{deal_id}/documents."""
    _seed_audit(owner_conn, seeded_org["org_pk"], seeded_deal, "analysis_started")

    other_clerk_org = f"test-audit-org-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) "
            "VALUES (%s, %s, now()) RETURNING id",
            (other_clerk_org, "Other Org"),
        )
        other_pk = cur.fetchone()[0]
    try:
        _authed(other_clerk_org, "user-2")
        resp = client.get(f"/deals/{seeded_deal}/audit")
        assert resp.status_code == 404
    finally:
        with owner_conn.cursor() as cur:
            for table in ("human_audit_log", "data_source", "deals", "users"):
                cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (other_pk,))
            cur.execute("DELETE FROM organisation WHERE id = %s", (other_pk,))
