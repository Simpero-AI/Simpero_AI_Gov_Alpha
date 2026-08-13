"""app/jobs/tasks/start_deal_screening.py -- the screening stage (SIM-404).

Runs the job function directly against real Postgres (owner_conn bypasses
RLS, same idiom as test_start_deal_verification_job.py). The decision engine
and evaluators are the REAL ones, not mocked: the point of these tests is
that the job, the engine and the claims/deal reads actually compose inside
one transaction under SET LOCAL.
"""

import importlib
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg2.errors
import pytest

job_module = importlib.import_module("app.jobs.tasks.start_deal_screening")


@pytest.fixture
def seeded_org(owner_conn) -> Iterator[dict[str, Any]]:
    clerk_org_id = f"test-tenant-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (clerk_org_id, "Screening Test Org"),
        )
        org_pk = cur.fetchone()[0]

    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk}

    with owner_conn.cursor() as cur:
        for table in ("screening_result", "human_audit_log", "claims", "analysis_run", "deals"):
            cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


def _seed_deal(owner_conn, org_pk: int, **fields) -> str:
    columns = ["org_id", "name", *fields]
    values = [org_pk, "Screening Test Deal", *fields.values()]
    placeholders = ", ".join(["%s"] * len(columns))
    with owner_conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO deals ({', '.join(columns)}) VALUES ({placeholders}) RETURNING id",
            values,
        )
        return str(cur.fetchone()[0])


def _seed_screening_run(owner_conn, org_pk: int, deal_id: str, status: str = "queued") -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_run (org_id, deal_id, job_name, status) "
            "VALUES (%s, %s, 'screening', %s) RETURNING id",
            (org_pk, deal_id, status),
        )
        return str(cur.fetchone()[0])


def _fetch_run(owner_conn, run_id: str) -> dict[str, Any]:
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT status, error_message, job_comments FROM analysis_run WHERE id = %s", (run_id,)
        )
        status, error_message, job_comments = cur.fetchone()
        return {"status": status, "error_message": error_message, "job_comments": job_comments}


def _fetch_results(owner_conn, org_pk: int) -> list[dict[str, Any]]:
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT recommendation, rulebook_version, rule_results, analysis_run_id "
            "FROM screening_result WHERE org_id = %s ORDER BY created_at",
            (org_pk,),
        )
        return [
            {
                "recommendation": r[0],
                "rulebook_version": r[1],
                "rule_results": r[2],
                "analysis_run_id": str(r[3]) if r[3] else None,
            }
            for r in cur.fetchall()
        ]


def _audit_events(owner_conn, org_pk: int) -> list[str]:
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT event_type FROM human_audit_log WHERE org_id = %s ORDER BY created_at",
            (org_pk,),
        )
        return [r[0] for r in cur.fetchall()]


# --- the happy paths --------------------------------------------------------


async def test_prohibited_sector_writes_an_auto_decline_result(owner_conn, seeded_org):
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"], sector="cannabis")
    run_id = _seed_screening_run(owner_conn, seeded_org["org_pk"], deal_id)

    await job_module.start_deal_screening(
        {}, analysis_run_id=run_id, clerk_org_id=seeded_org["clerk_org_id"]
    )

    run = _fetch_run(owner_conn, run_id)
    assert run["status"] == "successful"
    assert run["error_message"] is None

    results = _fetch_results(owner_conn, seeded_org["org_pk"])
    assert len(results) == 1
    assert results[0]["recommendation"] == "auto_decline"
    assert results[0]["rulebook_version"] == "track_b.v1"
    assert results[0]["analysis_run_id"] == run_id

    # The short-circuit survives into the persisted record, and the breaker
    # that fired is cited with its evidence.
    fired = results[0]["rule_results"][-1]
    assert fired["rule_id"] == "db_04"
    assert fired["verdict"] == "Y"
    assert fired["evidence_ref"] == {"kind": "deal_field", "field": "sector", "value": "cannabis"}

    assert "analysis_screening_completed" in _audit_events(owner_conn, seeded_org["org_pk"])

    # No job_comments: that column is a per-DOCUMENT shape and screening is
    # deal-level. Writing one broke GET /deals/{id}/status response
    # validation -- see test_deal_status_survives_a_screening_run.
    assert run["job_comments"] is None


async def test_a_bare_deal_reaches_human_review(owner_conn, seeded_org):
    """No claims, no metadata: every rule is `unknown`, which is neither an
    auto-decline nor a green light."""
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"])
    run_id = _seed_screening_run(owner_conn, seeded_org["org_pk"], deal_id)

    await job_module.start_deal_screening(
        {}, analysis_run_id=run_id, clerk_org_id=seeded_org["clerk_org_id"]
    )

    results = _fetch_results(owner_conn, seeded_org["org_pk"])
    assert results[0]["recommendation"] == "human_review"
    assert len(results[0]["rule_results"]) == 21
    assert all(r["verdict"] == "unknown" for r in results[0]["rule_results"])
    # An `unknown` carries no evidence and zero confidence -- never the 1.0
    # default, which would read as certainty in the audit trail.
    assert all(r["evidence_ref"] is None for r in results[0]["rule_results"])
    assert all(r["confidence"] == 0.0 for r in results[0]["rule_results"])


async def test_the_audit_payload_carries_the_full_per_rule_detail(owner_conn, seeded_org):
    """The append-only trail must stand on its own -- screening_result is a
    separate table that a future DDL change could alter."""
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"], sector="gambling")
    run_id = _seed_screening_run(owner_conn, seeded_org["org_pk"], deal_id)

    await job_module.start_deal_screening(
        {}, analysis_run_id=run_id, clerk_org_id=seeded_org["clerk_org_id"]
    )

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM human_audit_log WHERE org_id = %s "
            "AND event_type = 'analysis_screening_completed'",
            (seeded_org["org_pk"],),
        )
        payload = cur.fetchone()[0]

    assert payload["recommendation"] == "auto_decline"
    assert payload["rulebook_version"] == "track_b.v1"
    assert payload["rule_results"][-1]["rule_id"] == "db_04"
    # The human-readable line lives here now, not in job_comments.
    assert "db_04" in payload["summary"]


async def test_deal_status_survives_a_screening_run(owner_conn, seeded_org):
    """Regression: once screening became the deal's LATEST analysis_run, the
    status endpoint's `job_name == "verification"` block was a fallthrough
    that caught it, and passed the screening row's deal-level job_comments
    into JobCommentResponse (a per-document shape). That raised a
    ValidationError -- a 500 on GET /deals/{id}/status for every deal that
    finished screening. Builds the response the same way the handler does.
    """
    from app.schemas.deals import DealStatusResponse
    from app.services.pipeline_steps import no_job_steps

    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"], sector="cannabis")
    run_id = _seed_screening_run(owner_conn, seeded_org["org_pk"], deal_id)
    await job_module.start_deal_screening(
        {}, analysis_run_id=run_id, clerk_org_id=seeded_org["clerk_org_id"]
    )

    run = _fetch_run(owner_conn, run_id)
    # The exact hand-off that used to blow up.
    response = DealStatusResponse(
        job_status="processing",
        current_phase="governance",
        steps=[{**s, "status": "done"} for s in no_job_steps()],
        job_comments=run["job_comments"],
    )
    assert response.job_comments is None
    assert response.current_phase == "governance"


# --- failure and redelivery -------------------------------------------------


@pytest.mark.parametrize("terminal_status", ["successful", "failed"])
async def test_a_redelivered_job_on_a_terminal_run_is_a_no_op(
    owner_conn, seeded_org, terminal_status
):
    """SAQ can redeliver AFTER a successful commit. Rows here are
    append-only, so without the guard a redelivery would silently duplicate
    the record of a decision rather than failing loudly."""
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"], sector="cannabis")
    run_id = _seed_screening_run(owner_conn, seeded_org["org_pk"], deal_id, status=terminal_status)

    await job_module.start_deal_screening(
        {}, analysis_run_id=run_id, clerk_org_id=seeded_org["clerk_org_id"]
    )

    assert _fetch_results(owner_conn, seeded_org["org_pk"]) == []
    assert _fetch_run(owner_conn, run_id)["status"] == terminal_status


async def test_running_twice_writes_exactly_one_result(owner_conn, seeded_org):
    """The end-to-end version of the guard: the second call sees a run that
    the first call left `successful`."""
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"], sector="cannabis")
    run_id = _seed_screening_run(owner_conn, seeded_org["org_pk"], deal_id)

    for _ in range(2):
        await job_module.start_deal_screening(
            {}, analysis_run_id=run_id, clerk_org_id=seeded_org["clerk_org_id"]
        )

    assert len(_fetch_results(owner_conn, seeded_org["org_pk"])) == 1


async def test_a_missing_run_raises(seeded_org):
    """An unknown run id is a programming/queue error, not a screening
    outcome -- it must not be swallowed into a `human_review` result."""
    with pytest.raises(ValueError, match="not found"):
        await job_module.start_deal_screening(
            {}, analysis_run_id=str(uuid.uuid4()), clerk_org_id=seeded_org["clerk_org_id"]
        )


async def test_a_run_cannot_outlive_its_deal(owner_conn, seeded_org):
    """The job has a defensive "deal not found" -> failed branch, and this
    documents WHY that branch is unreachable rather than untested:
    analysis_run.deal_id is a plain FK with no ON DELETE, so the database
    refuses to orphan a run in the first place. Keeping the branch is cheap
    insurance against that FK ever being loosened; asserting the FK is what
    actually holds the invariant today.
    """
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"])
    _seed_screening_run(owner_conn, seeded_org["org_pk"], deal_id)

    with pytest.raises(psycopg2.errors.ForeignKeyViolation), owner_conn.cursor() as cur:
        cur.execute("DELETE FROM deals WHERE id = %s", (deal_id,))


async def test_screening_never_mutates_the_deal(owner_conn, seeded_org):
    """`auto_decline` is a RECOMMENDATION. Nothing in this job may decline
    the deal on its own -- acting on it is a human's call."""
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"], sector="cannabis")
    with owner_conn.cursor() as cur:
        cur.execute("SELECT status FROM deals WHERE id = %s", (deal_id,))
        before = cur.fetchone()[0]

    run_id = _seed_screening_run(owner_conn, seeded_org["org_pk"], deal_id)
    await job_module.start_deal_screening(
        {}, analysis_run_id=run_id, clerk_org_id=seeded_org["clerk_org_id"]
    )

    with owner_conn.cursor() as cur:
        cur.execute("SELECT status FROM deals WHERE id = %s", (deal_id,))
        assert cur.fetchone()[0] == before
