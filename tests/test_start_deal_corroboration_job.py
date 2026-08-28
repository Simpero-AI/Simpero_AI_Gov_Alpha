"""app/jobs/tasks/start_deal_corroboration.py -- the corroboration stage (SIM-416).

Runs the job function directly against real Postgres (owner_conn bypasses RLS,
same idiom as test_start_deal_screening_job.py). CORROBORATION_SOURCES is empty
in production today, so the gather phase is a no-op here too: these tests pin the
job's WIRING -- the two-phase transaction shape, the durable-failure guard, the
idempotency guard, and the chain into screening -- not any external source.
"""

import importlib
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

job_module = importlib.import_module("app.jobs.tasks.start_deal_corroboration")


@pytest.fixture
def mocked_screening_enqueue(monkeypatch: pytest.MonkeyPatch):
    """Mocks this module's own get_queue so a successful run's chain into
    start_deal_screening doesn't need a live Valkey -- same _FakeQueue idiom as
    the verification/analysis job tests."""
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
            (clerk_org_id, "Corroboration Test Org"),
        )
        org_pk = cur.fetchone()[0]

    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk}

    with owner_conn.cursor() as cur:
        for table in ("human_audit_log", "claims", "analysis_run", "deals"):
            cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


def _seed_deal(owner_conn, org_pk: int) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (org_pk, "Corroboration Test Deal"),
        )
        return str(cur.fetchone()[0])


def _seed_corroboration_run(owner_conn, org_pk: int, deal_id: str, status: str = "queued") -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_run (org_id, deal_id, job_name, status) "
            "VALUES (%s, %s, 'corroboration', %s) RETURNING id",
            (org_pk, deal_id, status),
        )
        return str(cur.fetchone()[0])


def _fetch_run(owner_conn, run_id: str) -> dict[str, Any]:
    with owner_conn.cursor() as cur:
        cur.execute("SELECT status, error_message FROM analysis_run WHERE id = %s", (run_id,))
        status, error_message = cur.fetchone()
        return {"status": status, "error_message": error_message}


def _screening_rows(owner_conn, deal_id: str) -> list[tuple[str, str]]:
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT id, status FROM analysis_run WHERE deal_id = %s AND job_name = 'screening'",
            (deal_id,),
        )
        return [(str(r[0]), r[1]) for r in cur.fetchall()]


def _audit_events(owner_conn, org_pk: int) -> list[str]:
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT event_type FROM human_audit_log WHERE org_id = %s ORDER BY created_at",
            (org_pk,),
        )
        return [r[0] for r in cur.fetchall()]


async def test_marks_successful_and_chains_into_screening(
    owner_conn, seeded_org, mocked_screening_enqueue
):
    """The happy path: with no registered sources the gather is a no-op, but the
    job still re-runs the roll-up, marks itself successful, and hands off to
    screening -- creating the screening run row inside its own transaction and
    enqueuing start_deal_screening after the commit."""
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"])
    run_id = _seed_corroboration_run(owner_conn, seeded_org["org_pk"], deal_id)

    await job_module.start_deal_corroboration(
        {}, analysis_run_id=run_id, clerk_org_id=seeded_org["clerk_org_id"]
    )

    assert _fetch_run(owner_conn, run_id)["status"] == "successful"

    screening = _screening_rows(owner_conn, deal_id)
    assert len(screening) == 1
    screening_run_id, screening_status = screening[0]
    assert screening_status == "queued"

    assert len(mocked_screening_enqueue) == 1
    job_name, kwargs = mocked_screening_enqueue[0]
    assert job_name == "start_deal_screening"
    assert kwargs["analysis_run_id"] == screening_run_id
    assert kwargs["clerk_org_id"] == seeded_org["clerk_org_id"]

    events = _audit_events(owner_conn, seeded_org["org_pk"])
    assert "analysis_corroboration_completed" in events
    assert "analysis_requested" in events


@pytest.mark.parametrize("terminal_status", ["successful", "failed"])
async def test_a_redelivered_job_on_a_terminal_run_is_a_no_op(
    owner_conn, seeded_org, mocked_screening_enqueue, terminal_status
):
    """SAQ can redeliver after a terminal commit. The idempotency guard must
    stop the pass re-running the gather or re-chaining a second screening row."""
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"])
    run_id = _seed_corroboration_run(
        owner_conn, seeded_org["org_pk"], deal_id, status=terminal_status
    )

    await job_module.start_deal_corroboration(
        {}, analysis_run_id=run_id, clerk_org_id=seeded_org["clerk_org_id"]
    )

    assert _fetch_run(owner_conn, run_id)["status"] == terminal_status
    assert _screening_rows(owner_conn, deal_id) == []
    assert mocked_screening_enqueue == []


async def test_a_missing_run_raises(seeded_org):
    """An unknown run id is a queue/programming error, not a corroboration
    outcome -- it must surface, not be swallowed."""
    with pytest.raises(ValueError, match="not found"):
        await job_module.start_deal_corroboration(
            {}, analysis_run_id=str(uuid.uuid4()), clerk_org_id=seeded_org["clerk_org_id"]
        )


async def test_a_midjob_failure_durably_marks_run_failed(
    owner_conn, seeded_org, mocked_screening_enqueue, monkeypatch
):
    """A raise in the write phase must land the run at a terminal `failed`
    status recorded in a FRESH transaction -- not roll back to a non-terminal
    marker that hangs the frontend on "loading results". And a crashed run must
    NOT chain onward. Mirrors the verify/screening durable-failure tests."""
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"])
    run_id = _seed_corroboration_run(owner_conn, seeded_org["org_pk"], deal_id)

    async def boom(session, claims):
        raise RuntimeError("rollup exploded")

    monkeypatch.setattr(job_module, "roll_up_deal", boom)

    with pytest.raises(RuntimeError, match="rollup exploded"):
        await job_module.start_deal_corroboration(
            {}, analysis_run_id=run_id, clerk_org_id=seeded_org["clerk_org_id"]
        )

    run = _fetch_run(owner_conn, run_id)
    assert run["status"] == "failed"
    assert run["error_message"] == "corroboration failed: RuntimeError"
    # Write transaction rolled back -> no screening chain, no enqueue.
    assert _screening_rows(owner_conn, deal_id) == []
    assert mocked_screening_enqueue == []
