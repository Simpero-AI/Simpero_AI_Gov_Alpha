"""Contract tests for POST /deals/{deal_id}/analysis and the updated
GET /deals/{deal_id}/status (docs/plans/start-analysis-flow-alpha.md).

Mirrors tests/test_phase1_endpoints.py's ApiTestClient/dependency_overrides
pattern against the real app. app.api.deals.get_queue is mocked at its call
site -- the same idiom test_uploads_api.py uses for uploads.get_queue -- so
no real Valkey connection is made; this test suite is about the HTTP
contract and DB state, not the worker task (see test_start_deal_analysis_job.py
for that).
"""

import hashlib
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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
        for table in (
            "human_audit_log",
            "analysis_run",
            "data_source",
            "deal_intake_link",
            "deals",
            "users",
        ):
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
    job_name: str = "parsing",
) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_run (org_id, deal_id, job_name, status, error_message, "
            "job_comments) VALUES (%s, %s, %s, %s, %s, %s::jsonb) RETURNING id",
            (org_pk, deal_id, job_name, status, error_message, json.dumps(job_comments)),
        )
        return str(cur.fetchone()[0])


def _token_hash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _seed_intake_link(
    owner_conn,
    org_pk: int,
    clerk_org_id: str,
    deal_id: str,
    status: str,
    expires_at: datetime,
) -> str:
    """Mirrors tests/test_intake_link_generate.py::_seed_pending_link
    (duplicated here per that module's own stated precedent for cross-file
    duplication, not shared), but parameterized by status. Always inserts as
    'pending' first (the column's server_default) and, when the target status
    isn't 'pending', UPDATEs to it afterward -- the same two-step pattern
    this file's own _seed_data_source helper uses for its status transitions;
    a single-step INSERT ... status=<terminal> would still work today, but
    the one-way-status trigger only fires on UPDATE, and mirroring the
    generate-then-transition path here keeps this helper consistent with how
    every non-pending row is actually produced in production. created_by_user_id
    points at a throwaway user row since the column is NOT NULL."""
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (org_id, role, login_method, clerk_user_id, clerk_org_id, status) "
            "VALUES (%s, 'admin', 'email', %s, %s, 'active') RETURNING id",
            (org_pk, f"seed-user-{uuid.uuid4().hex[:8]}", clerk_org_id),
        )
        user_pk = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO deal_intake_link "
            "(org_id, clerk_org_id, deal_id, token_hash, recipient_email, expires_at, "
            "created_by_user_id) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                org_pk,
                clerk_org_id,
                deal_id,
                _token_hash(f"seed-{uuid.uuid4().hex}"),
                "recipient@example.com",
                expires_at,
                user_pk,
            ),
        )
        link_id = cur.fetchone()[0]
        if status != "pending":
            cur.execute(
                "UPDATE deal_intake_link SET status = %s WHERE id = %s",
                (status, link_id),
            )
        return str(link_id)


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
    assert len(body["steps"]) == 2
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
    from app.jobs.tasks.start_deal_analysis import _PARSE_DEADLINE_PER_DOC_SECONDS

    # The SAQ job timeout MUST exceed the job's own inner poll deadline
    # (_PARSE_DEADLINE_PER_DOC_SECONDS per document, one usable doc here), or SAQ
    # hard-cancels the job before the deadline fires and strands the run
    # in_progress -- the freeze the deadline fix targets, relocated to the outer cap.
    assert kwargs["timeout"] == _PARSE_DEADLINE_PER_DOC_SECONDS + 600
    assert kwargs["timeout"] > _PARSE_DEADLINE_PER_DOC_SECONDS
    assert kwargs["retries"] == 1
    assert kwargs["ttl"] == 86400


def test_start_analysis_scales_the_saq_timeout_by_document_count(
    client, owner_conn, seeded_org, seeded_deal, mocked_queue
):
    """The SAQ job timeout/ttl scale with the document count, not a single-doc
    budget -- so the outer cap stays above the job's inner per-doc poll deadline
    (also * doc count) for a multi-document deal, not just a one-doc one."""
    from app.jobs.tasks.start_deal_analysis import _PARSE_DEADLINE_PER_DOC_SECONDS

    _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "verified")
    _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "verified")
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.post(f"/deals/{seeded_deal}/analysis", json={})
    assert resp.status_code == 202

    assert len(mocked_queue) == 1
    _job_name, kwargs = mocked_queue[0]
    # Two usable docs -> the multiplier is exercised: budget = per-doc * 2, still
    # strictly above the inner deadline's own per-doc * 2 for the same count.
    assert kwargs["timeout"] == _PARSE_DEADLINE_PER_DOC_SECONDS * 2 + 600
    assert kwargs["timeout"] > _PARSE_DEADLINE_PER_DOC_SECONDS * 2
    assert kwargs["ttl"] == max(86400, _PARSE_DEADLINE_PER_DOC_SECONDS * 2 + 3600)


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


# --- P3-14: pending intake link blocks start-analysis -----------------------


def test_start_analysis_409_when_intake_link_is_pending(
    client, owner_conn, seeded_org, seeded_deal, mocked_queue
):
    _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "verified")
    _seed_intake_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        seeded_deal,
        "pending",
        datetime.now(UTC) + timedelta(days=7),
    )
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.post(f"/deals/{seeded_deal}/analysis", json={})
    assert resp.status_code == 409
    assert (
        resp.json()["detail"]
        == "Cannot start analysis while an intake link is still pending for this deal"
    )

    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM analysis_run WHERE deal_id = %s", (seeded_deal,))
        assert cur.fetchone()[0] == 0
    assert not mocked_queue


def test_start_analysis_proceeds_when_pending_link_is_past_expiry(
    client, owner_conn, seeded_org, seeded_deal, mocked_queue
):
    link_id = _seed_intake_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        seeded_deal,
        "pending",
        datetime.now(UTC) - timedelta(hours=1),
    )
    _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "verified")
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.post(f"/deals/{seeded_deal}/analysis", json={})
    assert resp.status_code == 202
    assert len(mocked_queue) == 1

    # The guard read the row as effectively-expired via
    # compute_intake_link_effective_status without writing to it -- lazy
    # expiry, not a flip to 'expired' in storage.
    with owner_conn.cursor() as cur:
        cur.execute("SELECT status FROM deal_intake_link WHERE id = %s", (link_id,))
        assert cur.fetchone()[0] == "pending"


@pytest.mark.parametrize("link_status", ["submitted", "revoked", "expired"])
def test_start_analysis_proceeds_when_intake_link_is_terminal(
    client, owner_conn, seeded_org, seeded_deal, mocked_queue, link_status
):
    _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "verified")
    _seed_intake_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        seeded_deal,
        link_status,
        datetime.now(UTC) + timedelta(days=7),
    )
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
    assert body["startedAt"] is not None
    steps = {step["phase"]: step["status"] for step in body["steps"]}
    assert steps["parsing"] == "current"
    assert steps["verification"] == "pending"


def test_status_parsing_successful_run_maps_to_processing_verification_not_complete(
    client, owner_conn, seeded_org, seeded_deal
):
    """docs/plans/analysis-pipeline-stage-chaining.md point 4: extraction +
    the binding audit now happen inside the combined parsing job itself, so
    a successful parsing row points straight to verification -- and must never
    surface as "complete", since nothing downstream of verification has run yet
    either. Only "parsing"/"verification" are tracked steps at all (2026-08-12
    reduction) -- no phantom "classify"/"pass1" entries marked "done" for
    stages that never ran."""
    _seed_analysis_run(owner_conn, seeded_org["org_pk"], seeded_deal, "successful")
    _authed(seeded_org["clerk_org_id"], "user-1")

    body = client.get(f"/deals/{seeded_deal}/status").json()
    assert body["jobStatus"] == "processing"
    assert body["currentPhase"] == "verification"
    steps = {step["phase"]: step["status"] for step in body["steps"]}
    assert steps["parsing"] == "done"
    assert steps["verification"] == "current"


def test_status_verification_in_progress_maps_to_verification_current(
    client, owner_conn, seeded_org, seeded_deal
):
    _seed_analysis_run(
        owner_conn, seeded_org["org_pk"], seeded_deal, "in_progress", job_name="verification"
    )
    _authed(seeded_org["clerk_org_id"], "user-1")

    body = client.get(f"/deals/{seeded_deal}/status").json()
    assert body["jobStatus"] == "processing"
    assert body["currentPhase"] == "verification"
    steps = {step["phase"]: step["status"] for step in body["steps"]}
    assert steps["verification"] == "current"


def test_status_verification_successful_maps_to_complete(
    client, owner_conn, seeded_org, seeded_deal
):
    """A successful verification run is "complete" for a deal that hasn't
    reached screening yet -- once a screening row exists it supersedes
    verification as the latest run (see the screening-branch tests below),
    but until then there's no dedicated complete job or pipeline stage past
    it (the memo tail: governance/OFAC/drafting/scoring has no job behind
    it), so verification succeeding stands in as terminal. currentPhase
    stays "governance" regardless -- _steps_for_status already reports
    every tracked step ("parsing", "verification") as "done" once
    currentPhase is past both, which is the correct steps array for a
    finished deal on its own; only the top-level jobStatus needed to
    change."""
    _seed_analysis_run(
        owner_conn, seeded_org["org_pk"], seeded_deal, "successful", job_name="verification"
    )
    _authed(seeded_org["clerk_org_id"], "user-1")

    body = client.get(f"/deals/{seeded_deal}/status").json()
    assert body["jobStatus"] == "complete"
    assert body["currentPhase"] == "governance"
    steps = {step["phase"]: step["status"] for step in body["steps"]}
    assert steps["parsing"] == "done"
    assert steps["verification"] == "done"


def test_status_screening_successful_maps_to_complete(client, owner_conn, seeded_org, seeded_deal):
    """SIM-401/402/403/404: screening is the real last stage in the chain,
    so once a screening row exists (superseding verification as the
    latest run), its own "successful" status -- not verification's -- is
    what maps to job_status="complete"."""
    _seed_analysis_run(
        owner_conn, seeded_org["org_pk"], seeded_deal, "successful", job_name="verification"
    )
    _seed_analysis_run(
        owner_conn, seeded_org["org_pk"], seeded_deal, "successful", job_name="screening"
    )
    _authed(seeded_org["clerk_org_id"], "user-1")

    body = client.get(f"/deals/{seeded_deal}/status").json()
    assert body["jobStatus"] == "complete"
    assert body["currentPhase"] == "governance"
    steps = {step["phase"]: step["status"] for step in body["steps"]}
    assert steps["parsing"] == "done"
    assert steps["verification"] == "done"


def test_status_screening_in_progress_maps_to_processing(
    client, owner_conn, seeded_org, seeded_deal
):
    _seed_analysis_run(
        owner_conn, seeded_org["org_pk"], seeded_deal, "successful", job_name="verification"
    )
    _seed_analysis_run(
        owner_conn, seeded_org["org_pk"], seeded_deal, "in_progress", job_name="screening"
    )
    _authed(seeded_org["clerk_org_id"], "user-1")

    body = client.get(f"/deals/{seeded_deal}/status").json()
    assert body["jobStatus"] == "processing"
    assert body["currentPhase"] == "governance"


def test_status_screening_failed_maps_to_error(client, owner_conn, seeded_org, seeded_deal):
    _seed_analysis_run(
        owner_conn, seeded_org["org_pk"], seeded_deal, "successful", job_name="verification"
    )
    _seed_analysis_run(
        owner_conn,
        seeded_org["org_pk"],
        seeded_deal,
        "failed",
        "Screening could not complete.",
        job_name="screening",
    )
    _authed(seeded_org["clerk_org_id"], "user-1")

    body = client.get(f"/deals/{seeded_deal}/status").json()
    assert body["jobStatus"] == "error"
    assert body["currentPhase"] == "governance"


def test_status_verification_failed_maps_to_verification_failed(
    client, owner_conn, seeded_org, seeded_deal
):
    _seed_analysis_run(
        owner_conn,
        seeded_org["org_pk"],
        seeded_deal,
        "failed",
        "No documents were successfully extracted to verify.",
        job_name="verification",
    )
    _authed(seeded_org["clerk_org_id"], "user-1")

    body = client.get(f"/deals/{seeded_deal}/status").json()
    assert body["jobStatus"] == "error"
    assert body["currentPhase"] == "verification"
    steps = {step["phase"]: step["status"] for step in body["steps"]}
    assert steps["verification"] == "failed"


def _seed_timed_run(
    owner_conn,
    org_pk: int,
    deal_id: str,
    job_name: str,
    status: str,
    started_at: str,
    ended_at: str | None,
) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_run (org_id, deal_id, job_name, status, started_at, ended_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (org_pk, deal_id, job_name, status, started_at, ended_at),
        )
        return str(cur.fetchone()[0])


def test_status_verification_successful_includes_real_chain_timing(
    client, owner_conn, seeded_org, seeded_deal
):
    """startedAt is the CHAIN's start (the parsing run's), not the
    verification row's own started_at; stepDurations carries each step's
    own real wall time, derived from each run's own started_at/ended_at --
    nothing here is guessed or client-side."""
    _seed_timed_run(
        owner_conn,
        seeded_org["org_pk"],
        seeded_deal,
        "parsing",
        "successful",
        "2026-08-12T07:00:00+00:00",
        "2026-08-12T07:00:20+00:00",
    )
    _seed_timed_run(
        owner_conn,
        seeded_org["org_pk"],
        seeded_deal,
        "verification",
        "successful",
        "2026-08-12T07:00:25+00:00",
        "2026-08-12T07:01:10+00:00",
    )
    _authed(seeded_org["clerk_org_id"], "user-1")

    body = client.get(f"/deals/{seeded_deal}/status").json()
    assert body["currentPhase"] == "governance"
    assert body["startedAt"] == "2026-08-12T07:00:00Z"
    assert body["endedAt"] == "2026-08-12T07:01:10Z"
    assert body["stepDurations"] == {"parsing": 20, "verification": 45}


def test_status_verification_in_progress_omits_verification_duration(
    client, owner_conn, seeded_org, seeded_deal
):
    """While the verification run itself hasn't ended yet, its step must
    not appear in stepDurations at all -- there is no real duration to
    report yet, and the endpoint must never guess one."""
    _seed_timed_run(
        owner_conn,
        seeded_org["org_pk"],
        seeded_deal,
        "parsing",
        "successful",
        "2026-08-12T07:00:00+00:00",
        "2026-08-12T07:00:20+00:00",
    )
    _seed_timed_run(
        owner_conn,
        seeded_org["org_pk"],
        seeded_deal,
        "verification",
        "in_progress",
        "2026-08-12T07:00:25+00:00",
        None,
    )
    _authed(seeded_org["clerk_org_id"], "user-1")

    body = client.get(f"/deals/{seeded_deal}/status").json()
    assert body["currentPhase"] == "verification"
    assert body["startedAt"] == "2026-08-12T07:00:00Z"
    assert body["endedAt"] is None
    assert body["stepDurations"] == {"parsing": 20}


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
