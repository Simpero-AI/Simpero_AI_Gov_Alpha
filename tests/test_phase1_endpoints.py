"""Endpoint-level tests for Phase 1: deals.get/pipeline/dashboard-stats/status,
history list/get/delete/clearAll, investment-profile.get, and the auth flow
(me/sync-profile/logout) after its UserRepo refactor.

Hits the real app through TestClient with only `get_claims` overridden (skips
real Clerk verification) — `get_db` runs for real: SET LOCAL, JIT
provisioning, RLS, the lot. Organisations are pre-seeded via the doadmin
connection so JIT provisioning doesn't need to reach Clerk's Backend API for
a brand-new org; the auth-flow test is the one place that path (UserRepo
.upsert on a genuinely new user) is deliberately exercised.
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
    """Prepends /api — every route is mounted there (app/main.py) to match
    the frontend's dev proxy + prod ingress, both keyed on /api/*. Keeps the
    test bodies below reading as the bare route paths."""

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
    """A fresh org, seeded directly (bypasses RLS) — real Clerk org lookup
    is never exercised here except in the dedicated auth-flow test."""
    clerk_org_id = f"test-tenant-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (clerk_org_id, "Endpoint Test Org"),
        )
        org_pk = cur.fetchone()[0]

    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk}

    with owner_conn.cursor() as cur:
        for table in ("human_audit_log", "sessions", "investment_profiles", "deals", "users"):
            cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


def _seed_deal(owner_conn, org_pk: int, **fields: Any) -> str:
    columns = {"org_id": org_pk, "name": "Acme Deal", "gp_source": "Acme Capital", **fields}
    cols = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    with owner_conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO deals ({cols}) VALUES ({placeholders}) RETURNING id",
            list(columns.values()),
        )
        return str(cur.fetchone()[0])


def _seed_session(owner_conn, org_pk: int, deal_id: str, **fields: Any) -> str:
    columns = {
        "org_id": org_pk,
        "deal_id": deal_id,
        "file_name": "deck.pdf",
        **fields,
    }
    cols = ", ".join(columns)
    placeholders = ", ".join("%s::jsonb" if k == "memo_json" else "%s" for k in columns)
    values = [json.dumps(v) if k == "memo_json" else v for k, v in columns.items()]
    with owner_conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO sessions ({cols}) VALUES ({placeholders}) RETURNING id",
            values,
        )
        return str(cur.fetchone()[0])


# --- deals -------------------------------------------------------------


def test_get_deal_without_session(client, owner_conn, seeded_org):
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"], sector_tags=json.dumps(["fintech"]))
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get(f"/deals/{deal_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deal"]["id"] == deal_id
    assert body["deal"]["state"] == "sourcing"
    assert json.loads(body["deal"]["sectorTags"]) == ["fintech"]
    assert body["latestMemoSession"] is None


def test_get_deal_with_session(client, owner_conn, seeded_org):
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"])
    memo = {"scorecard": {"claimsExtracted": 10}}
    session_id = _seed_session(owner_conn, seeded_org["org_pk"], deal_id, memo_json=memo)
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get(f"/deals/{deal_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["latestMemoSession"]["sessionId"] == session_id
    assert json.loads(body["latestMemoSession"]["memoJson"]) == memo


def test_get_deal_404_when_missing(client, seeded_org):
    _authed(seeded_org["clerk_org_id"], "user-1")
    resp = client.get(f"/deals/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_get_deal_audits_document_access(client, owner_conn, seeded_org):
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"])
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get(f"/deals/{deal_id}")
    assert resp.status_code == 200

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT actor_id, event_type FROM human_audit_log WHERE org_id = %s AND deal_id = %s",
            (seeded_org["org_pk"], deal_id),
        )
        rows = cur.fetchall()
        assert [(r[0], r[1]) for r in rows] == [("user-1", "document_access")]


def test_get_deal_404_does_not_audit(client, owner_conn, seeded_org):
    """A miss (RLS or genuinely absent) isn't an access — nothing was read,
    so no document_access row should be written."""
    _authed(seeded_org["clerk_org_id"], "user-1")
    resp = client.get(f"/deals/{uuid.uuid4()}")
    assert resp.status_code == 404

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM human_audit_log "
            "WHERE org_id = %s AND event_type = 'document_access'",
            (seeded_org["org_pk"],),
        )
        assert cur.fetchone()[0] == 0


def test_get_deal_status_no_job_shape(client, owner_conn, seeded_org):
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"])
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get(f"/deals/{deal_id}/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["jobStatus"] == "no_job"
    assert body["currentPhase"] is None
    assert len(body["steps"]) == 2
    assert all(step["status"] == "pending" for step in body["steps"])


def test_list_pipeline_and_dashboard_stats_shapes(client, owner_conn, seeded_org):
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"])
    _authed(seeded_org["clerk_org_id"], "user-1")

    pipeline_resp = client.get("/deals/pipeline")
    assert pipeline_resp.status_code == 200
    rows = pipeline_resp.json()
    assert any(row["dealId"] == deal_id for row in rows)
    row = next(row for row in rows if row["dealId"] == deal_id)
    assert row["sectorTags"] == []
    assert row["agentStatus"]["jobStatus"] == "no_job"

    stats_resp = client.get("/deals/dashboard-stats")
    assert stats_resp.status_code == 200
    stats = stats_resp.json()
    assert stats["window"] == "month"
    assert stats["totalDeals"]["value"] >= 1
    assert "pipelineValueUsd" in stats
    assert stats["avgAiScore"]["value"] is None
    assert stats["ddCompletionPct"]["value"] == 0


# --- history -------------------------------------------------------------


def test_history_list_get_delete(client, owner_conn, seeded_org):
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"])
    memo = {"scorecard": {"claimsExtracted": 5, "claimsMatched": 4, "matchRate": 80}}
    session_id = _seed_session(owner_conn, seeded_org["org_pk"], deal_id, memo_json=memo)
    _authed(seeded_org["clerk_org_id"], "user-1")

    list_resp = client.get("/history")
    assert list_resp.status_code == 200
    summaries = list_resp.json()
    assert any(s["sessionId"] == session_id for s in summaries)
    summary = next(s for s in summaries if s["sessionId"] == session_id)
    assert summary["claimsExtracted"] == 5
    assert summary["matchRate"] == 80

    get_resp = client.get(f"/history/{session_id}")
    assert get_resp.status_code == 200
    got = get_resp.json()
    assert got["dealId"] == deal_id
    assert got["memo"] == memo

    delete_resp = client.delete(f"/history/{session_id}")
    assert delete_resp.status_code == 200
    assert delete_resp.json()["success"] is True

    # Gone afterwards.
    assert client.get(f"/history/{session_id}").json() is None

    with owner_conn.cursor() as cur:
        cur.execute("SELECT event_type FROM human_audit_log WHERE session_id = %s", (session_id,))
        assert cur.fetchone()[0] == "memo_deleted"


def test_history_get_returns_null_when_missing(client, seeded_org):
    _authed(seeded_org["clerk_org_id"], "user-1")
    resp = client.get(f"/history/{uuid.uuid4()}")
    assert resp.status_code == 200
    assert resp.json() is None


def test_history_clear_all(client, owner_conn, seeded_org):
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"])
    _seed_session(owner_conn, seeded_org["org_pk"], deal_id)
    _seed_session(owner_conn, seeded_org["org_pk"], deal_id)
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.delete("/history")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["deletedCount"] == 2

    assert client.get("/history").json() == []

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM human_audit_log WHERE org_id = %s AND event_type = %s",
            (seeded_org["org_pk"], "history_cleared"),
        )
        assert cur.fetchone()[0]["deleted_count"] == 2


# --- investment profile ---------------------------------------------------


def test_investment_profile_null_when_absent(client, seeded_org):
    _authed(seeded_org["clerk_org_id"], "user-1")
    resp = client.get("/investment-profile")
    assert resp.status_code == 200
    assert resp.json() is None


def test_investment_profile_present(client, owner_conn, seeded_org):
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO investment_profiles (org_id, firm_name, mandate, weights) "
            "VALUES (%s, %s, %s::jsonb, %s::jsonb)",
            (
                seeded_org["org_pk"],
                "Acme Capital",
                json.dumps({"checkSize": "5-10m"}),
                json.dumps({}),
            ),
        )
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get("/investment-profile")
    assert resp.status_code == 200
    body = resp.json()
    assert body["firmName"] == "Acme Capital"
    assert body["mandate"] == {"checkSize": "5-10m"}


# --- auth flow (post UserRepo refactor) -----------------------------------


def test_auth_flow_me_sync_profile_logout(client, owner_conn, seeded_org):
    """Exercises the JIT-provisioning path (UserRepo.get_by_clerk_id miss ->
    UserRepo.upsert) for a genuinely new user, then sync-profile and logout —
    the three routes refactored onto UserRepo."""
    clerk_user_id = f"test-user-{uuid.uuid4().hex[:8]}"
    _authed(seeded_org["clerk_org_id"], clerk_user_id)

    me_resp = client.get("/auth/me")
    assert me_resp.status_code == 200
    me_body = me_resp.json()
    assert me_body["name"] is None
    assert me_body["email"] is None

    sync_resp = client.post(
        "/auth/sync-profile", json={"name": "Jane Analyst", "email": "jane@example.com"}
    )
    assert sync_resp.status_code == 200
    assert sync_resp.json()["success"] is True

    me_resp_2 = client.get("/auth/me")
    assert me_resp_2.json()["email"] == "jane@example.com"

    logout_resp = client.post("/auth/logout")
    assert logout_resp.status_code == 200
    assert logout_resp.json()["success"] is True

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT actor_email FROM human_audit_log WHERE event_type = 'auth_sign_out' "
            "AND org_id = %s",
            (seeded_org["org_pk"],),
        )
        assert cur.fetchone()[0] == "jane@example.com"

        # Two auth_login rows: one per /auth/me call above (before and after
        # sync-profile) — logging on every call, not just first-ever login,
        # since Clerk sign-in is client-side and /auth/me is the only
        # server-side checkpoint that fires per session.
        cur.execute(
            "SELECT actor_id FROM human_audit_log WHERE event_type = 'auth_login' "
            "AND org_id = %s ORDER BY created_at",
            (seeded_org["org_pk"],),
        )
        assert [r[0] for r in cur.fetchall()] == [clerk_user_id, clerk_user_id]


def test_auth_login_audit_records_actor_and_org(client, owner_conn, seeded_org):
    clerk_user_id = f"test-user-{uuid.uuid4().hex[:8]}"
    _authed(seeded_org["clerk_org_id"], clerk_user_id)

    resp = client.get("/auth/me")
    assert resp.status_code == 200

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT actor_id, actor_email, event_type FROM human_audit_log "
            "WHERE org_id = %s AND event_type = 'auth_login'",
            (seeded_org["org_pk"],),
        )
        rows = cur.fetchall()
        assert [(r[0], r[1], r[2]) for r in rows] == [(clerk_user_id, None, "auth_login")]


# --- logs ------------------------------------------------------------------


def test_recent_activity_shape_and_limit(client, owner_conn, seeded_org):
    org_pk = seeded_org["org_pk"]
    # Explicit spaced-out created_at so desc ordering is deterministic
    # regardless of how fast these inserts actually run.
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO human_audit_log (org_id, event_type, created_at) VALUES "
            "(%s, 'memo_saved', now() - interval '3 seconds'), "
            "(%s, 'memo_deliverable_patched', now() - interval '2 seconds'), "
            "(%s, 'auth_sign_out', now() - interval '1 seconds')",
            (org_pk, org_pk, org_pk),
        )
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get("/logs/recent-activity", params={"limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    # total/warnings/critical are over the whole trail, not just `limit` rows.
    assert body["total"] == 3
    assert body["warnings"] == 1
    assert body["critical"] == 1
    assert len(body["rows"]) == 2
    assert body["rows"][0]["action"] == "auth_sign_out"
    assert body["rows"][1]["action"] == "memo_deliverable_patched"
    assert body["rows"][0]["jobId"] is None
