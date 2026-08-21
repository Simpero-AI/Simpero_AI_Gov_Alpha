"""SIM-413: the status roll-up (SIM-254) actually running inside the verify
job.

tests/test_status_rollup.py already drives the rule itself over its whole
input matrix. This file covers the wiring, which is where the product change
lives: until now `roll_up_status` had no caller, so trust status never moved
and every claim ended a run at whatever the parser said. Three things have to
hold for the wiring to be honest, and each has its own test below:

1. It runs, and a cleanly-cited claim comes out `verified`.
2. It runs AFTER 3a/3b, so a claim those passes contradicted comes out
   `inconclusive` -- and therefore drops out of screening trust. That
   demotion is SIM-254's pinned, intended behaviour, and it is the sharp
   edge of this ticket: a claim screening could see at `cited` disappears
   the moment the roll-up runs over it.
3. It never raises on the claims it has no verdict for. `roll_up_status`
   raises ClaimNotCorroboratableError on anything short of `cited`, and a
   real run is full of `missing`/`proposed` claims -- an unguarded loop
   would fail the whole verification job on the first one.
"""

import importlib
from collections.abc import Iterator
from typing import Any

import pytest

# The package's __init__ rebinds this name to the job FUNCTION, so a plain
# `import ... as job_module` yields the function, not the module -- same idiom
# as tests/test_start_deal_verification_job.py.
job_module = importlib.import_module("app.jobs.tasks.start_deal_verification")


@pytest.fixture
def mocked_screening_enqueue(monkeypatch: pytest.MonkeyPatch):
    """The job chains into screening on success; the queue is not under test."""
    calls: list[tuple[str, dict]] = []

    class _FakeQueue:
        async def enqueue(self, job_name: str, **kwargs: Any) -> None:
            calls.append((job_name, kwargs))

    monkeypatch.setattr(job_module, "get_queue", lambda: _FakeQueue())
    return calls


@pytest.fixture
def seeded_org(owner_conn) -> Iterator[dict[str, Any]]:
    clerk_org_id = "test-tenant-rollup-wiring"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (clerk_org_id) DO NOTHING",
            (clerk_org_id, "Rollup Wiring Org"),
        )
        cur.execute("SELECT id FROM organisation WHERE clerk_org_id = %s", (clerk_org_id,))
        org_pk = cur.fetchone()[0]
    try:
        yield {"org_pk": org_pk, "clerk_org_id": clerk_org_id}
    finally:
        with owner_conn.cursor() as cur:
            cur.execute("DELETE FROM edges WHERE org_id = %s", (org_pk,))
            cur.execute("DELETE FROM claims WHERE org_id = %s", (org_pk,))
            cur.execute("DELETE FROM analysis_run WHERE org_id = %s", (org_pk,))
            cur.execute("DELETE FROM human_audit_log WHERE org_id = %s", (org_pk,))
            cur.execute("DELETE FROM data_source WHERE org_id = %s", (org_pk,))
            cur.execute("DELETE FROM deals WHERE org_id = %s", (org_pk,))
            cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


@pytest.fixture
def seeded_deal(owner_conn, seeded_org) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (seeded_org["org_pk"], "Rollup Wiring Deal"),
        )
        return str(cur.fetchone()[0])


def _seed_data_source(owner_conn, org_pk: int, deal_id: str) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO data_source (org_id, deal_id, storage_key, filename, declared_sha256) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (org_pk, deal_id, "org/cim.pdf", "cim.pdf", "0" * 64),
        )
        return str(cur.fetchone()[0])


def _seed_runs(owner_conn, org_pk: int, deal_id: str, parse_jobs: list[dict]) -> tuple[str, str]:
    """The parsing run (holding the parse_jobs manifest) and the verification
    run the job is invoked for. uq_analysis_run_active is per DEAL, so the
    parsing run is seeded terminal."""
    import json

    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_run (org_id, deal_id, job_name, status, parse_jobs) "
            "VALUES (%s, %s, 'parsing', 'successful', %s) RETURNING id",
            (org_pk, deal_id, json.dumps(parse_jobs)),
        )
        parsing_run_id = str(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO analysis_run (org_id, deal_id, job_name, status) "
            "VALUES (%s, %s, 'verification', 'queued') RETURNING id",
            (org_pk, deal_id),
        )
        return parsing_run_id, str(cur.fetchone()[0])


def _claim_json(
    claim_ref: str, page: int, *, normalized: float = 100.0, attribute: str = "revenue"
) -> dict[str, Any]:
    return {
        "claim_ref": claim_ref,
        "claim_type": "numerical",
        "entity": "Acme Corp",
        "attribute": attribute,
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


def _parse_jobs(data_source_id: str) -> list[dict]:
    return [
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


def _statuses(owner_conn, org_pk: int) -> dict[str, int]:
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT status, count(*) FROM claims WHERE org_id = %s GROUP BY status", (org_pk,)
        )
        return dict(cur.fetchall())


def _run_payload(owner_conn, run_id: str) -> dict[str, Any]:
    with owner_conn.cursor() as cur:
        cur.execute("SELECT status, job_comments FROM analysis_run WHERE id = %s", (run_id,))
        status, job_comments = cur.fetchone()
        return {"status": status, "job_comments": job_comments}


async def _run_job(monkeypatch, envelope, seeded_org, parsing_run_id, run_id):
    monkeypatch.setattr(job_module, "get_json_object", lambda bucket, key: envelope)
    await job_module.start_deal_verification(
        {},
        analysis_run_id=run_id,
        parsing_run_id=parsing_run_id,
        clerk_org_id=seeded_org["clerk_org_id"],
    )


async def test_cleanly_cited_claims_come_out_verified(
    owner_conn, seeded_org, seeded_deal, monkeypatch, mocked_screening_enqueue
):
    """The acceptance criterion: a run used to end with zero `verified`
    claims because nothing called the roll-up. `exact_span` is a strong
    method, so a promoted claim with no internal disagreement and no external
    check resolves all the way to `verified`."""
    data_source_id = _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal)
    parsing_run_id, run_id = _seed_runs(
        owner_conn, seeded_org["org_pk"], seeded_deal, _parse_jobs(data_source_id)
    )
    envelope = {
        "run_id": parsing_run_id,
        "sha256": "a" * 64,
        "source_file": "cim.pdf",
        # Two unrelated attributes -> no same-fact grouping, no contradicts.
        "claims": [
            _claim_json("c1", page=1, attribute="revenue"),
            _claim_json("c2", page=5, attribute="ebitda", normalized=42.0),
        ],
        "edges": [],
    }

    await _run_job(monkeypatch, envelope, seeded_org, parsing_run_id, run_id)

    run = _run_payload(owner_conn, run_id)
    assert run["status"] == "successful"
    assert _statuses(owner_conn, seeded_org["org_pk"]) == {"verified": 2}
    assert "Status roll-up (deal-wide): 2 verified" in run["job_comments"][0]["comment"]


async def test_contradicted_claims_are_demoted_out_of_screening_trust(
    owner_conn, seeded_org, seeded_deal, monkeypatch, mocked_screening_enqueue
):
    """The roll-up must run AFTER 3a/3b, since a `contradicts` edge is its
    internal-disagreement signal. Two different values for the same
    entity/attribute/period on different pages is exactly what 3a contradicts,
    so both claims must land on `inconclusive` -- and `inconclusive` is not in
    claims_lookup's trusted set, so neither reaches a screening evaluator.
    Running the roll-up before 3a would have left both `verified`."""
    data_source_id = _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal)
    parsing_run_id, run_id = _seed_runs(
        owner_conn, seeded_org["org_pk"], seeded_deal, _parse_jobs(data_source_id)
    )
    envelope = {
        "run_id": parsing_run_id,
        "sha256": "a" * 64,
        "source_file": "cim.pdf",
        "claims": [
            _claim_json("c1", page=1, normalized=100.0),
            _claim_json("c2", page=5, normalized=900.0),
        ],
        "edges": [],
    }

    await _run_job(monkeypatch, envelope, seeded_org, parsing_run_id, run_id)

    with owner_conn.cursor() as cur:
        cur.execute("SELECT type FROM edges WHERE org_id = %s", (seeded_org["org_pk"],))
        assert ("contradicts",) in cur.fetchall()

    assert _statuses(owner_conn, seeded_org["org_pk"]) == {"inconclusive": 2}

    from app.services.screening.claims_lookup import _TRUSTED_STATUSES

    assert "inconclusive" not in _TRUSTED_STATUSES


async def test_unpromotable_claims_do_not_fail_the_job(
    owner_conn, seeded_org, seeded_deal, monkeypatch, mocked_screening_enqueue
):
    """roll_up_status raises ClaimNotCorroboratableError on anything short of
    `cited`, and a real run is full of such claims -- a `missing` one the
    resolver could not locate, a `proposed` one the binding auditor faulted.
    The status filter on the loop is what keeps them out; without it the whole
    verification job fails on the first one."""
    data_source_id = _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal)
    parsing_run_id, run_id = _seed_runs(
        owner_conn, seeded_org["org_pk"], seeded_deal, _parse_jobs(data_source_id)
    )

    faulted = _claim_json("c2", page=5, attribute="ebitda")
    faulted["flags"] = ["binding_unsupported"]
    not_found = _claim_json("c3", page=9, attribute="headcount")
    not_found["status"] = "missing"
    not_found["location"] = {"kind": "pdf", "page": 9}

    envelope = {
        "run_id": parsing_run_id,
        "sha256": "a" * 64,
        "source_file": "cim.pdf",
        "claims": [_claim_json("c1", page=1), faulted, not_found],
        "edges": [],
    }

    await _run_job(monkeypatch, envelope, seeded_org, parsing_run_id, run_id)

    assert _run_payload(owner_conn, run_id)["status"] == "successful"
    assert _statuses(owner_conn, seeded_org["org_pk"]) == {
        "verified": 1,
        "proposed": 1,
        "missing": 1,
    }


async def test_rolled_up_statuses_are_recorded_on_the_audit_event(
    owner_conn, seeded_org, seeded_deal, monkeypatch, mocked_screening_enqueue
):
    """The roll-up is where a claim silently leaves screening trust, so the
    run has to say what it did -- an auditor asking "why did this deal screen
    on one figure instead of three?" needs the answer in the audit log, not
    only in the claims table's current state."""
    data_source_id = _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal)
    parsing_run_id, run_id = _seed_runs(
        owner_conn, seeded_org["org_pk"], seeded_deal, _parse_jobs(data_source_id)
    )
    envelope = {
        "run_id": parsing_run_id,
        "sha256": "a" * 64,
        "source_file": "cim.pdf",
        "claims": [_claim_json("c1", page=1)],
        "edges": [],
    }

    await _run_job(monkeypatch, envelope, seeded_org, parsing_run_id, run_id)

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM human_audit_log WHERE org_id = %s "
            "AND event_type = 'analysis_verification_completed'",
            (seeded_org["org_pk"],),
        )
        payload = cur.fetchone()[0]
    assert payload["status_rollup"] == {"verified": 1}


async def test_rolling_up_again_over_the_same_claims_is_idempotent(
    owner_conn, seeded_org, seeded_deal, monkeypatch, mocked_screening_enqueue
):
    """A resolved status is itself corroboratable, so the second pass re-reads
    the same claims rather than skipping them -- it must land on the same
    answer. This is the reason the roll-up can be re-run as external
    corroboration arrives later without disturbing settled claims."""
    from sqlalchemy import text

    from app.core.database import AsyncSessionLocal
    from scripts.run_verification import _roll_up_all

    data_source_id = _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal)
    parsing_run_id, run_id = _seed_runs(
        owner_conn, seeded_org["org_pk"], seeded_deal, _parse_jobs(data_source_id)
    )
    envelope = {
        "run_id": parsing_run_id,
        "sha256": "a" * 64,
        "source_file": "cim.pdf",
        "claims": [
            _claim_json("c1", page=1, normalized=100.0),
            _claim_json("c2", page=5, normalized=900.0),
            _claim_json("c3", page=7, attribute="ebitda", normalized=42.0),
        ],
        "edges": [],
    }

    await _run_job(monkeypatch, envelope, seeded_org, parsing_run_id, run_id)
    after_first = _statuses(owner_conn, seeded_org["org_pk"])
    assert after_first == {"inconclusive": 2, "verified": 1}

    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.org_id', :k, true)"),
            {"k": seeded_org["clerk_org_id"]},
        )
        second = await _roll_up_all(session)

    assert second == {"inconclusive": 2, "verified": 1}
    assert _statuses(owner_conn, seeded_org["org_pk"]) == after_first
