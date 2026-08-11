"""Contract tests for POST /deals/{deal_id}/analysis and the updated
GET /deals/{deal_id}/status (docs/plans/start-analysis-flow-alpha.md).

Mirrors tests/test_phase1_endpoints.py's ApiTestClient/dependency_overrides
pattern against the real app. app.api.deals.get_queue is mocked at its call
site -- the same idiom test_uploads_api.py uses for uploads.get_queue -- so
no real Valkey connection is made; this test suite is about the HTTP
contract and DB state, not the worker task (see test_start_deal_analysis_job.py
for that).
"""

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import deals
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
def mocked_queue(monkeypatch: pytest.MonkeyPatch):
    enqueue_calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeQueue:
        async def enqueue(self, job_name: str, **kwargs: Any) -> None:
            enqueue_calls.append((job_name, kwargs))
            return None

    monkeypatch.setattr(deals, "get_queue", lambda: _FakeQueue())
    return enqueue_calls


@pytest.fixture
def seeded_org(owner_conn) -> Iterator[dict[str, Any]]:
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
        for table in ("human_audit_log", "analysis_run", "data_source", "deals", "users"):
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


def _seed_data_source(owner_conn, org_pk: int, deal_id: str, status: str, **fields: Any) -> str:
    columns = {
        "org_id": org_pk,
        "deal_id": deal_id,
        "storage_key": f"org/{uuid.uuid4().hex[:8]}.pdf",
        "filename": "financials.pdf",
        "declared_sha256": uuid.uuid4().hex + uuid.uuid4().hex,
        **fields,
    }
    cols = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    with owner_conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO data_source ({cols}) VALUES ({placeholders}) RETURNING id",
            list(columns.values()),
        )
        data_source_id = cur.fetchone()[0]
        if status != "pending":
            cur.execute(
                "UPDATE data_source SET status = %s, status_updated_at = now() WHERE id = %s",
                (status, data_source_id),
            )
        return str(data_source_id)


def _seed_analysis_run(
    owner_conn,
    org_pk: int,
    deal_id: str,
    status: str,
    error_message: str | None = None,
    job_comments: list[dict[str, Any]] | None = None,
) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_run (org_id, deal_id, status, error_message, job_comments) "
            "VALUES (%s, %s, %s, %s, %s::jsonb) RETURNING id",
            (org_pk, deal_id, status, error_message, json.dumps(job_comments)),
        )
        return str(cur.fetchone()[0])


# --- POST /deals/{deal_id}/analysis ----------------------------------------


def test_start_analysis_404_when_deal_missing(client, seeded_org):
    _authed(seeded_org["clerk_org_id"], "user-1")
    resp = client.post(f"/deals/{uuid.uuid4()}/analysis", json={})
    assert resp.status_code == 404


def test_start_analysis_422_when_no_documents(client, seeded_org, seeded_deal, mocked_queue):
    _authed(seeded_org["clerk_org_id"], "user-1")
    resp = client.post(f"/deals/{seeded_deal}/analysis", json={})
    assert resp.status_code == 422
    assert not mocked_queue


def test_start_analysis_409_when_only_pending_documents(
    client, owner_conn, seeded_org, seeded_deal, mocked_queue
):
    _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "pending")
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.post(f"/deals/{seeded_deal}/analysis", json={})
    assert resp.status_code == 409
    assert "still being verified" in resp.json()["detail"]
    assert not mocked_queue


def test_start_analysis_happy_path(client, owner_conn, seeded_org, seeded_deal, mocked_queue):
    _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "verified")
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.post(f"/deals/{seeded_deal}/analysis", json={"selectedFrameworks": ["nist"]})
    assert resp.status_code == 202
    body = resp.json()
    assert body["jobStatus"] == "queued"
    assert body["currentPhase"] is None
    assert len(body["steps"]) == 9
    assert all(step["status"] == "pending" for step in body["steps"])

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT status, selected_frameworks, job_name FROM analysis_run WHERE deal_id = %s",
            (seeded_deal,),
        )
        row = cur.fetchone()
        assert row[0] == "queued"
        assert row[1] == ["nist"]
        assert row[2] == "parsing"

        cur.execute(
            "SELECT event_type, payload FROM human_audit_log "
            "WHERE org_id = %s AND event_type = 'analysis_requested'",
            (seeded_org["org_pk"],),
        )
        audit_row = cur.fetchone()
        assert audit_row is not None
        assert audit_row[1]["document_count"] == 1

    assert len(mocked_queue) == 1
    job_name, kwargs = mocked_queue[0]
    assert job_name == "start_deal_analysis"
    assert kwargs["deal_id"] == seeded_deal
    assert kwargs["clerk_org_id"] == seeded_org["clerk_org_id"]
    assert kwargs["timeout"] == 7200
    assert kwargs["retries"] == 1
    assert kwargs["ttl"] == 86400


def test_start_analysis_409_when_a_run_is_already_active(
    client, owner_conn, seeded_org, seeded_deal, mocked_queue
):
    _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "verified")
    _seed_analysis_run(owner_conn, seeded_org["org_pk"], seeded_deal, "in_progress")
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.post(f"/deals/{seeded_deal}/analysis", json={})
    assert resp.status_code == 409
    assert "already running" in resp.json()["detail"]
    assert not mocked_queue

    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM analysis_run WHERE deal_id = %s", (seeded_deal,))
        assert cur.fetchone()[0] == 1


def test_start_analysis_allowed_once_prior_run_is_terminal(
    client, owner_conn, seeded_org, seeded_deal, mocked_queue
):
    _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "verified")
    _seed_analysis_run(owner_conn, seeded_org["org_pk"], seeded_deal, "failed", "boom")
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.post(f"/deals/{seeded_deal}/analysis", json={})
    assert resp.status_code == 202
    assert len(mocked_queue) == 1


# --- GET /deals/{deal_id}/status, mapped from the latest analysis_run -----


def test_status_no_run_is_no_job(client, seeded_org, seeded_deal):
    _authed(seeded_org["clerk_org_id"], "user-1")
    resp = client.get(f"/deals/{seeded_deal}/status")
    assert resp.status_code == 200
    assert resp.json()["jobStatus"] == "no_job"


def test_status_queued_run(client, owner_conn, seeded_org, seeded_deal):
    _seed_analysis_run(owner_conn, seeded_org["org_pk"], seeded_deal, "queued")
    _authed(seeded_org["clerk_org_id"], "user-1")

    body = client.get(f"/deals/{seeded_deal}/status").json()
    assert body["jobStatus"] == "queued"
    assert body["currentPhase"] is None
    assert all(step["status"] == "pending" for step in body["steps"])


def test_status_in_progress_run(client, owner_conn, seeded_org, seeded_deal):
    _seed_analysis_run(owner_conn, seeded_org["org_pk"], seeded_deal, "in_progress")
    _authed(seeded_org["clerk_org_id"], "user-1")

    body = client.get(f"/deals/{seeded_deal}/status").json()
    assert body["jobStatus"] == "processing"
    assert body["currentPhase"] == "parsing"
    steps = {step["phase"]: step["status"] for step in body["steps"]}
    assert steps["parsing"] == "current"
    assert steps["classify"] == "pending"


def test_status_successful_run_maps_to_processing_classify_not_complete(
    client, owner_conn, seeded_org, seeded_deal
):
    """D14: successful must never surface as "complete" -- classification
    hasn't run yet, so the frontend would render an empty memo tab."""
    _seed_analysis_run(owner_conn, seeded_org["org_pk"], seeded_deal, "successful")
    _authed(seeded_org["clerk_org_id"], "user-1")

    body = client.get(f"/deals/{seeded_deal}/status").json()
    assert body["jobStatus"] == "processing"
    assert body["currentPhase"] == "classify"
    steps = {step["phase"]: step["status"] for step in body["steps"]}
    assert steps["parsing"] == "done"
    assert steps["classify"] == "current"
    assert steps["pass1"] == "pending"


def test_status_failed_run(client, owner_conn, seeded_org, seeded_deal):
    _seed_analysis_run(
        owner_conn, seeded_org["org_pk"], seeded_deal, "failed", "All 2 documents need OCR."
    )
    _authed(seeded_org["clerk_org_id"], "user-1")

    body = client.get(f"/deals/{seeded_deal}/status").json()
    assert body["jobStatus"] == "error"
    assert body["currentPhase"] == "parsing"
    assert body["errorMessage"] == "All 2 documents need OCR."
    steps = {step["phase"]: step["status"] for step in body["steps"]}
    assert steps["parsing"] == "failed"


def test_status_surfaces_job_comments_only_once_terminal(
    client, owner_conn, seeded_org, seeded_deal
):
    """job_comments is the frontend-facing findings summary -- null while
    queued/in_progress, populated on the terminal (successful/failed) run."""
    _seed_analysis_run(owner_conn, seeded_org["org_pk"], seeded_deal, "in_progress")
    _authed(seeded_org["clerk_org_id"], "user-1")
    assert client.get(f"/deals/{seeded_deal}/status").json()["jobComments"] is None

    comments = [
        {"dataSourceId": "abc", "fileName": "deck.pdf", "status": "parsed", "comment": "ok"}
    ]
    _seed_analysis_run(
        owner_conn, seeded_org["org_pk"], seeded_deal, "successful", job_comments=comments
    )

    body = client.get(f"/deals/{seeded_deal}/status").json()
    assert body["jobComments"] == comments


def test_status_reflects_latest_run_not_an_earlier_one(client, owner_conn, seeded_org, seeded_deal):
    _seed_analysis_run(owner_conn, seeded_org["org_pk"], seeded_deal, "failed", "first try")
    _seed_analysis_run(owner_conn, seeded_org["org_pk"], seeded_deal, "in_progress")
    _authed(seeded_org["clerk_org_id"], "user-1")

    body = client.get(f"/deals/{seeded_deal}/status").json()
    assert body["jobStatus"] == "processing"
    assert body["currentPhase"] == "parsing"
