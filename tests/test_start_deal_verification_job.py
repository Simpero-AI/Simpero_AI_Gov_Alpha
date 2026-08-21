"""app/jobs/tasks/start_deal_verification.py -- the ingest+verify job
(docs/plans/analysis-pipeline-stage-chaining.md, points 2-3).

Runs the job function directly against real Postgres (owner_conn bypasses
RLS, same idiom as test_start_deal_analysis_job.py).
app.jobs.tasks.start_deal_verification's get_json_object is monkeypatched at
its own call site -- no real Spaces/network call. reconcile_same_fact and
reconcile_consistency are the REAL functions, not mocked: this test verifies
genuine end-to-end integration (ingest, then those passes reading back what
was just inserted in the same transaction), not just that they get called.
"""

import importlib
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

job_module = importlib.import_module("app.jobs.tasks.start_deal_verification")


@pytest.fixture
def mocked_screening_enqueue(monkeypatch: pytest.MonkeyPatch):
    """Mocks this module's own get_queue so a successful run's chain into
    start_deal_screening (SIM-404) doesn't need a live Valkey. Same
    _FakeQueue idiom as test_start_deal_analysis_job.py's
    mocked_verification_enqueue, one stage further along the chain."""
    enqueue_calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeQueue:
        async def enqueue(self, job_name: str, **kwargs: Any) -> None:
            enqueue_calls.append((job_name, kwargs))

    monkeypatch.setattr(job_module, "get_queue", lambda: _FakeQueue())
    return enqueue_calls


@pytest.fixture
def seeded_org(owner_conn) -> Iterator[dict[str, Any]]:
    clerk_org_id = f"test-tenant-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (clerk_org_id, "Verify Test Org"),
        )
        org_pk = cur.fetchone()[0]

    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk}

    with owner_conn.cursor() as cur:
        for table in ("human_audit_log", "edges", "claims", "analysis_run", "data_source", "deals"):
            cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


@pytest.fixture
def seeded_deal(owner_conn, seeded_org) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (seeded_org["org_pk"], "Verify Test Deal"),
        )
        return str(cur.fetchone()[0])


def _seed_data_source(owner_conn, org_pk: int, deal_id: str, filename: str) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO data_source (org_id, deal_id, storage_key, filename, declared_sha256) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (org_pk, deal_id, f"org/{filename}", filename, "a" * 64),
        )
        return str(cur.fetchone()[0])


def _seed_parsing_run(owner_conn, org_pk: int, deal_id: str, parse_jobs: list[dict]) -> str:
    import json

    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_run (org_id, deal_id, job_name, status, parse_jobs) "
            "VALUES (%s, %s, 'parsing', 'successful', %s::jsonb) RETURNING id",
            (org_pk, deal_id, json.dumps(parse_jobs)),
        )
        return str(cur.fetchone()[0])


def _seed_verification_run(owner_conn, org_pk: int, deal_id: str) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_run (org_id, deal_id, job_name, status) "
            "VALUES (%s, %s, 'verification', 'queued') RETURNING id",
            (org_pk, deal_id),
        )
        return str(cur.fetchone()[0])


def _claim_json(claim_ref: str, page: int, normalized: float = 100.0) -> dict[str, Any]:
    return {
        "claim_ref": claim_ref,
        "claim_type": "numerical",
        "entity": "Acme Corp",
        "attribute": "revenue",
        "value": {
            "raw": str(normalized),
            "normalized": normalized,
            "unit": "USD",
            "value_type": "currency",
            "scale_multiplier": 1.0,
            "scale_source": "assumed_1x",
        },
        "status": "proposed",
        "location": {"kind": "pdf", "page": page, "char_start": 0, "char_end": 10},
        "period_year": 2025,
        "period_kind": "A",
    }


def _fetch_run(owner_conn, run_id: str) -> dict[str, Any]:
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT status, error_message, job_comments FROM analysis_run WHERE id = %s",
            (run_id,),
        )
        status, error_message, job_comments = cur.fetchone()
        return {"status": status, "error_message": error_message, "job_comments": job_comments}


def _count_claims(owner_conn, org_pk: int) -> int:
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM claims WHERE org_id = %s", (org_pk,))
        return cur.fetchone()[0]


def _claim_statuses(owner_conn, org_pk: int) -> dict[str, int]:
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT status, count(*) FROM claims WHERE org_id = %s GROUP BY status", (org_pk,)
        )
        return dict(cur.fetchall())


def _fetch_edges(owner_conn, org_pk: int) -> list[tuple[str, str]]:
    with owner_conn.cursor() as cur:
        cur.execute("SELECT type, created_by FROM edges WHERE org_id = %s", (org_pk,))
        return cur.fetchall()


async def test_ingests_claims_and_reconciles_same_page_fact(
    owner_conn, seeded_org, seeded_deal, monkeypatch, mocked_screening_enqueue
):
    data_source_id = _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "cim.pdf")
    parse_jobs = [
        {
            "data_source_id": data_source_id,
            "filename": "cim.pdf",
            "storage_key": "org/cim.pdf",
            "job_key": "job-1",
            "outcome": "parsed",
            "code": None,
            "message": None,
            "bucket": "test-bucket",
            "key": "claims/cim.json",
        }
    ]
    parsing_run_id = _seed_parsing_run(owner_conn, seeded_org["org_pk"], seeded_deal, parse_jobs)
    run_id = _seed_verification_run(owner_conn, seeded_org["org_pk"], seeded_deal)

    envelope = {
        "run_id": parsing_run_id,
        "sha256": "a" * 64,
        "source_file": "cim.pdf",
        # Same entity/attribute/period, same value, different pages -> a
        # genuine cross-page same_fact case for reconcile_same_fact.
        "claims": [_claim_json("c1", page=1), _claim_json("c2", page=5)],
        "edges": [],
    }

    def fake_get_json_object(bucket: str, key: str) -> dict:
        assert bucket == "test-bucket"
        assert key == "claims/cim.json"
        return envelope

    monkeypatch.setattr(job_module, "get_json_object", fake_get_json_object)

    await job_module.start_deal_verification(
        {},
        analysis_run_id=run_id,
        parsing_run_id=parsing_run_id,
        clerk_org_id=seeded_org["clerk_org_id"],
    )

    run = _fetch_run(owner_conn, run_id)
    assert run["status"] == "successful"
    assert run["error_message"] is None
    assert _count_claims(owner_conn, seeded_org["org_pk"]) == 2

    edges = _fetch_edges(owner_conn, seeded_org["org_pk"])
    assert ("same_fact", "reconciliation") in edges

    assert run["job_comments"][0]["dataSourceId"] == data_source_id
    assert run["job_comments"][0]["status"] == "verified"
    assert "2 claim(s) ingested" in run["job_comments"][0]["comment"]
    assert "1 same_fact" in run["job_comments"][0]["comment"]
    assert "2 cited via exact_span" in run["job_comments"][0]["comment"]

    # SIM-404: chains into a job_name="screening" row + enqueue. The row is
    # created inside the job's transaction (uq_analysis_run_active is per
    # DEAL, not per job_name, so it can only exist once this run is
    # terminal), while the enqueue happens after the commit -- so by the time
    # the enqueue is recorded the row must already be readable.
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT id, status FROM analysis_run WHERE deal_id = %s AND job_name = 'screening'",
            (seeded_deal,),
        )
        screening_rows = cur.fetchall()
    assert len(screening_rows) == 1
    screening_run_id, screening_status = screening_rows[0]
    assert screening_status == "queued"

    assert len(mocked_screening_enqueue) == 1
    job_name, kwargs = mocked_screening_enqueue[0]
    assert job_name == "start_deal_screening"
    assert kwargs["analysis_run_id"] == str(screening_run_id)
    assert kwargs["clerk_org_id"] == seeded_org["clerk_org_id"]


async def test_promotes_span_resolved_claims_and_holds_the_rest(
    owner_conn, seeded_org, seeded_deal, monkeypatch, mocked_screening_enqueue
):
    """SIM-412 through the real job: the parser hands every PDF claim over at
    `proposed`, and before this pass existed the deal ended the run 100%
    `proposed`/`missing` with zero `cited` -- nothing screening or any external
    corroborator would look at. tests/test_span_promotion.py covers the rule
    itself; this pins that the job actually runs it, and that the two claims it
    must NOT promote survive a full end-to-end run untouched."""
    data_source_id = _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "cim.pdf")
    parse_jobs = [
        {
            "data_source_id": data_source_id,
            "filename": "cim.pdf",
            "storage_key": "org/cim.pdf",
            "job_key": "job-1",
            "outcome": "parsed",
            "code": None,
            "message": None,
            "bucket": "test-bucket",
            "key": "claims/cim.json",
        }
    ]
    parsing_run_id = _seed_parsing_run(owner_conn, seeded_org["org_pk"], seeded_deal, parse_jobs)
    run_id = _seed_verification_run(owner_conn, seeded_org["org_pk"], seeded_deal)

    faulted = _claim_json("c2", page=5)
    faulted["flags"] = ["binding_unsupported"]

    not_found = _claim_json("c3", page=9)
    not_found["status"] = "missing"
    not_found["location"] = {"kind": "pdf", "page": 9}

    envelope = {
        "run_id": parsing_run_id,
        "sha256": "a" * 64,
        "source_file": "cim.pdf",
        "claims": [_claim_json("c1", page=1), faulted, not_found],
        "edges": [],
    }
    monkeypatch.setattr(job_module, "get_json_object", lambda bucket, key: envelope)

    await job_module.start_deal_verification(
        {},
        analysis_run_id=run_id,
        parsing_run_id=parsing_run_id,
        clerk_org_id=seeded_org["clerk_org_id"],
    )

    assert _fetch_run(owner_conn, run_id)["status"] == "successful"
    # `cited` is where the promoter leaves c1, but it is not where the run
    # ends: SIM-413's roll-up runs after 3a/3b and carries a cleanly-cited,
    # strongly-verified claim on to `verified`. The two claims the promoter
    # refused stay exactly where they were, which is what this test is for.
    assert _claim_statuses(owner_conn, seeded_org["org_pk"]) == {
        "verified": 1,
        "proposed": 1,
        "missing": 1,
    }

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT claim_ref, verification_method FROM claims WHERE org_id = %s "
            "ORDER BY claim_ref",
            (seeded_org["org_pk"],),
        )
        assert cur.fetchall() == [("c1", "exact_span"), ("c2", None), ("c3", None)]

    comment = _fetch_run(owner_conn, run_id)["job_comments"][0]["comment"]
    assert "1 cited via exact_span" in comment
    assert "1 held at proposed (binding_unsupported)" in comment


async def test_no_usable_documents_marks_run_failed(owner_conn, seeded_org, seeded_deal):
    parsing_run_id = _seed_parsing_run(owner_conn, seeded_org["org_pk"], seeded_deal, [])
    run_id = _seed_verification_run(owner_conn, seeded_org["org_pk"], seeded_deal)

    await job_module.start_deal_verification(
        {},
        analysis_run_id=run_id,
        parsing_run_id=parsing_run_id,
        clerk_org_id=seeded_org["clerk_org_id"],
    )

    run = _fetch_run(owner_conn, run_id)
    assert run["status"] == "failed"
    assert "extracted" in run["error_message"]
    assert _count_claims(owner_conn, seeded_org["org_pk"]) == 0


async def test_midjob_failure_durably_marks_run_failed(
    owner_conn, seeded_org, seeded_deal, monkeypatch, mocked_screening_enqueue
):
    """The stuck-forever bug: the whole job runs in one transaction, so a
    mid-job crash rolls back the in_progress marker too, leaving the run
    non-terminal and the UI spinning indefinitely. The wrapper must record a
    terminal `failed` status in a FRESH transaction. Proven by claims=0 (the
    work transaction rolled back) AND status=failed (a separate transaction
    committed the terminal state)."""
    data_source_id = _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "cim.pdf")
    parse_jobs = [
        {
            "data_source_id": data_source_id,
            "filename": "cim.pdf",
            "storage_key": "org/cim.pdf",
            "job_key": "job-1",
            "outcome": "parsed",
            "code": None,
            "message": None,
            "bucket": "test-bucket",
            "key": "claims/cim.json",
        }
    ]
    parsing_run_id = _seed_parsing_run(owner_conn, seeded_org["org_pk"], seeded_deal, parse_jobs)
    run_id = _seed_verification_run(owner_conn, seeded_org["org_pk"], seeded_deal)

    envelope = {
        "run_id": parsing_run_id,
        "sha256": "a" * 64,
        "source_file": "cim.pdf",
        "claims": [_claim_json("c1", page=1), _claim_json("c2", page=5)],
        "edges": [],
    }
    monkeypatch.setattr(job_module, "get_json_object", lambda bucket, key: envelope)

    # Blow up AFTER ingest, during reconciliation -- the realistic failure
    # point, and the one that used to strand the run at in_progress forever.
    def boom(*args, **kwargs):
        raise RuntimeError("reconcile exploded")

    monkeypatch.setattr(job_module, "reconcile_same_fact", boom)

    with pytest.raises(RuntimeError, match="reconcile exploded"):
        await job_module.start_deal_verification(
            {},
            analysis_run_id=run_id,
            parsing_run_id=parsing_run_id,
            clerk_org_id=seeded_org["clerk_org_id"],
        )

    run = _fetch_run(owner_conn, run_id)
    assert run["status"] == "failed"
    assert run["error_message"] == "verification failed: RuntimeError"
    # Work transaction rolled back -> no claims committed; the failed status
    # came from a SEPARATE transaction. That split is the whole fix.
    assert _count_claims(owner_conn, seeded_org["org_pk"]) == 0
    # A crashed run must not chain into screening.
    assert len(mocked_screening_enqueue) == 0


async def test_already_terminal_run_is_a_noop(owner_conn, seeded_org, seeded_deal, monkeypatch):
    """D11-style idempotency guard (review on PR #81): a SAQ redelivery after
    a successful commit must not re-run the insert-only ingest -- it would
    hit uq_claims_org_data_source_claim_ref on rows already committed and
    hard-fail the retry, leaving the run stuck."""
    data_source_id = _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "cim.pdf")
    parse_jobs = [
        {
            "data_source_id": data_source_id,
            "filename": "cim.pdf",
            "storage_key": "org/cim.pdf",
            "job_key": "job-1",
            "outcome": "parsed",
            "code": None,
            "message": None,
            "bucket": "test-bucket",
            "key": "claims/cim.json",
        }
    ]
    parsing_run_id = _seed_parsing_run(owner_conn, seeded_org["org_pk"], seeded_deal, parse_jobs)
    run_id = _seed_verification_run(owner_conn, seeded_org["org_pk"], seeded_deal)
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE analysis_run SET status = 'successful' WHERE id = %s", (run_id,))

    def fail_if_called(bucket: str, key: str) -> dict:
        raise AssertionError("get_json_object must not be called once the run is terminal")

    monkeypatch.setattr(job_module, "get_json_object", fail_if_called)

    await job_module.start_deal_verification(
        {},
        analysis_run_id=run_id,
        parsing_run_id=parsing_run_id,
        clerk_org_id=seeded_org["clerk_org_id"],
    )

    assert _count_claims(owner_conn, seeded_org["org_pk"]) == 0
    run = _fetch_run(owner_conn, run_id)
    assert run["status"] == "successful"


async def test_missing_run_raises(owner_conn, seeded_org, seeded_deal):
    parsing_run_id = _seed_parsing_run(owner_conn, seeded_org["org_pk"], seeded_deal, [])
    with pytest.raises(ValueError, match="not found"):
        await job_module.start_deal_verification(
            {},
            analysis_run_id=str(uuid.uuid4()),
            parsing_run_id=parsing_run_id,
            clerk_org_id=seeded_org["clerk_org_id"],
        )
