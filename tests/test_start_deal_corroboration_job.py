"""app/jobs/tasks/start_deal_corroboration.py -- the external-source pass, run
run-row-less BETWEEN verification and screening. Runs the job directly against real
Postgres (owner_conn bypasses RLS). DEFAULT_SOURCES is monkeypatched with a fake
source (no network) and get_queue is monkeypatched (no Valkey), so this exercises
the real 3-phase DB flow: read claims -> gather verdicts -> persist events + re-roll,
then hand off to screening.
"""

import importlib
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from app.services.corroboration import CorroborationVerdict

job_module = importlib.import_module("app.jobs.tasks.start_deal_corroboration")


class _FakeSource:
    """A CorroborationSource that returns a fixed verdict (or raises) with no
    network. `check` ignores db/claim -- enough to drive the persist + roll-up."""

    def __init__(self, name: str, verdict: CorroborationVerdict | None, *, raises: bool = False):
        self.name = name
        self._verdict = verdict
        self._raises = raises

    async def check(self, db: Any, claim: Any) -> CorroborationVerdict | None:
        if self._raises:
            raise RuntimeError("source blew up")
        return self._verdict


@pytest.fixture
def mocked_enqueue(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeQueue:
        async def enqueue(self, job_name: str, **kwargs: Any) -> None:
            calls.append((job_name, kwargs))

    monkeypatch.setattr(job_module, "get_queue", lambda: _FakeQueue())
    return calls


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
        for table in (
            "corroboration_events",
            "human_audit_log",
            "edges",
            "claims",
            "analysis_run",
            "deals",
        ):
            cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


@pytest.fixture
def seeded_deal(owner_conn, seeded_org) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (seeded_org["org_pk"], "Corroboration Test Deal"),
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


def _seed_claim(owner_conn, org_pk: int, deal_id: str, *, status: str = "verified") -> str:
    with owner_conn.cursor() as cur:
        # char_start/char_end are required for a span-located claim (ck_claims_found_requires_span:
        # a non-'missing' pdf claim must carry a span); exact_span is a strong method.
        cur.execute(
            "INSERT INTO claims (org_id, deal_id, entity, attribute, value, kind, page, char_start, "
            "char_end, status, verification_method) VALUES (%s, %s, 'AcmeCo', 'revenue', %s::jsonb, "
            "'pdf', 1, 0, 10, %s, 'exact_span') RETURNING id",
            (
                org_pk,
                deal_id,
                json.dumps(
                    {"raw": "100", "normalized": 100, "unit": "USD", "value_type": "currency"}
                ),
                status,
            ),
        )
        return str(cur.fetchone()[0])


def _claim_status(owner_conn, claim_id: str) -> str:
    with owner_conn.cursor() as cur:
        cur.execute("SELECT status FROM claims WHERE id = %s", (claim_id,))
        return cur.fetchone()[0]


def _events_for(owner_conn, claim_id: str) -> list[tuple[str, dict]]:
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT outside_source, result FROM corroboration_events WHERE claim_id = %s",
            (claim_id,),
        )
        return cur.fetchall()


async def test_records_a_disagreement_and_hands_off_to_screening(
    owner_conn, seeded_org, seeded_deal, monkeypatch, mocked_enqueue
):
    claim_id = _seed_claim(owner_conn, seeded_org["org_pk"], seeded_deal, status="verified")
    screening_run_id = _seed_screening_run(owner_conn, seeded_org["org_pk"], seeded_deal)
    monkeypatch.setattr(
        job_module,
        "DEFAULT_SOURCES",
        [
            _FakeSource(
                "fake_source", CorroborationVerdict(agrees=False, result={"finding": "mismatch"})
            )
        ],
    )

    await job_module.start_deal_corroboration(
        {}, screening_run_id=screening_run_id, clerk_org_id=seeded_org["clerk_org_id"]
    )

    # The disagreement is recorded as an append-only event...
    events = _events_for(owner_conn, claim_id)
    assert events == [("fake_source", {"finding": "mismatch"})]
    # ...the claim is flipped to conflicted (and the re-run roll-up keeps it there)...
    assert _claim_status(owner_conn, claim_id) == "conflicted"
    # ...and screening is enqueued against the same run row.
    assert len(mocked_enqueue) == 1
    job_name, kwargs = mocked_enqueue[0]
    assert job_name == "start_deal_screening"
    assert kwargs["analysis_run_id"] == screening_run_id
    assert kwargs["clerk_org_id"] == seeded_org["clerk_org_id"]


async def test_no_op_when_screening_run_is_no_longer_queued(
    owner_conn, seeded_org, seeded_deal, monkeypatch, mocked_enqueue
):
    # A SAQ redelivery after the pass already handed off: the screening row is past
    # `queued`, so corroboration must NOT re-run (no duplicate append-only events) and
    # must NOT re-enqueue screening.
    claim_id = _seed_claim(owner_conn, seeded_org["org_pk"], seeded_deal, status="verified")
    screening_run_id = _seed_screening_run(
        owner_conn, seeded_org["org_pk"], seeded_deal, status="in_progress"
    )
    monkeypatch.setattr(
        job_module,
        "DEFAULT_SOURCES",
        [
            _FakeSource(
                "fake_source", CorroborationVerdict(agrees=False, result={"finding": "mismatch"})
            )
        ],
    )

    await job_module.start_deal_corroboration(
        {}, screening_run_id=screening_run_id, clerk_org_id=seeded_org["clerk_org_id"]
    )

    assert _events_for(owner_conn, claim_id) == []
    assert _claim_status(owner_conn, claim_id) == "verified"
    assert mocked_enqueue == []


async def test_still_hands_off_to_screening_when_a_source_raises(
    owner_conn, seeded_org, seeded_deal, monkeypatch, mocked_enqueue
):
    # Best-effort enrichment: a flaky source is no-signal, records nothing, and never
    # blocks the pipeline -- screening is still enqueued.
    claim_id = _seed_claim(owner_conn, seeded_org["org_pk"], seeded_deal, status="verified")
    screening_run_id = _seed_screening_run(owner_conn, seeded_org["org_pk"], seeded_deal)
    monkeypatch.setattr(
        job_module, "DEFAULT_SOURCES", [_FakeSource("fake_source", None, raises=True)]
    )

    await job_module.start_deal_corroboration(
        {}, screening_run_id=screening_run_id, clerk_org_id=seeded_org["clerk_org_id"]
    )

    assert _events_for(owner_conn, claim_id) == []
    assert _claim_status(owner_conn, claim_id) == "verified"
    assert len(mocked_enqueue) == 1
    assert mocked_enqueue[0][0] == "start_deal_screening"
