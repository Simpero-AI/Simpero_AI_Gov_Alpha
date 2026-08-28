"""app/jobs/tasks/start_deal_analysis.py -- the fan-out worker task
(docs/plans/start-analysis-flow-alpha.md, reworked per
docs/plans/analysis-pipeline-stage-chaining.md's point 4: parsing and
extraction are one combined job now, and a successful run chains straight
into start_deal_verification).

Runs the job function directly against real Postgres (owner_conn bypasses
RLS, same idiom as test_ingest_data_source.py). app.jobs.parse_client's
enqueue_process_document_job/get_parse_job, and app.jobs.tasks.
start_deal_analysis's get_queue, are monkeypatched at start_deal_analysis's
own call sites -- no real Valkey/parser-service call. Every fake job here
resolves COMPLETE on the first poll, so the loop never actually calls
asyncio.sleep.
"""

import importlib
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from saq.job import Status

job_module = importlib.import_module("app.jobs.tasks.start_deal_analysis")


class _FakeSaqJob:
    def __init__(self, status: Status, result: dict[str, Any] | None = None):
        self.status = status
        self.result = result


@pytest.fixture
def mocked_verification_enqueue(monkeypatch: pytest.MonkeyPatch):
    """Mocks app.jobs.tasks.start_deal_analysis's own get_queue (the
    "simpero" queue) so a successful run's chain into start_deal_verification
    never opens a real Valkey connection -- same idiom
    test_start_analysis_endpoint.py's mocked_queue fixture uses for the
    request handler's own enqueue."""
    enqueue_calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeQueue:
        async def enqueue(self, job_name: str, **kwargs: Any) -> None:
            enqueue_calls.append((job_name, kwargs))
            return None

    monkeypatch.setattr(job_module, "get_queue", lambda: _FakeQueue())
    return enqueue_calls


@pytest.fixture
def seeded_org(owner_conn) -> Iterator[dict[str, Any]]:
    clerk_org_id = f"test-tenant-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (clerk_org_id, "Job Test Org"),
        )
        org_pk = cur.fetchone()[0]

    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk}

    with owner_conn.cursor() as cur:
        for table in ("human_audit_log", "analysis_run", "data_source", "deals"):
            cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


@pytest.fixture
def seeded_deal(owner_conn, seeded_org) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (seeded_org["org_pk"], "Job Test Deal"),
        )
        return str(cur.fetchone()[0])


def _seed_verified_data_source(owner_conn, org_pk: int, deal_id: str, storage_key: str) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO data_source (org_id, deal_id, storage_key, filename, declared_sha256) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (org_pk, deal_id, storage_key, "file.pdf", "a" * 64),
        )
        data_source_id = cur.fetchone()[0]
        cur.execute(
            "UPDATE data_source SET status = 'verified', fingerprint = %s, "
            "status_updated_at = now() WHERE id = %s",
            ("a" * 64, data_source_id),
        )
        return str(data_source_id)


def _seed_run(owner_conn, org_pk: int, deal_id: str) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_run (org_id, deal_id, job_name) VALUES (%s, %s, 'parsing') "
            "RETURNING id",
            (org_pk, deal_id),
        )
        return str(cur.fetchone()[0])


def _fetch_run(owner_conn, run_id: str) -> dict[str, Any]:
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT status, parse_jobs, error_message, started_at, ended_at, job_comments "
            "FROM analysis_run WHERE id = %s",
            (run_id,),
        )
        status, parse_jobs, error_message, started_at, ended_at, job_comments = cur.fetchone()
        return {
            "status": status,
            "parse_jobs": parse_jobs,
            "error_message": error_message,
            "started_at": started_at,
            "ended_at": ended_at,
            "job_comments": job_comments,
        }


def _fetch_data_source_status(owner_conn, data_source_id: str) -> str:
    with owner_conn.cursor() as cur:
        cur.execute("SELECT status FROM data_source WHERE id = %s", (data_source_id,))
        return cur.fetchone()[0]


def _count_analysis_runs(owner_conn, deal_id: str, job_name: str) -> int:
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM analysis_run WHERE deal_id = %s AND job_name = %s",
            (deal_id, job_name),
        )
        return cur.fetchone()[0]


async def test_all_documents_parsed_marks_run_successful(
    owner_conn, seeded_org, seeded_deal, monkeypatch, mocked_verification_enqueue
):
    data_source_id = _seed_verified_data_source(
        owner_conn, seeded_org["org_pk"], seeded_deal, "org/a.pdf"
    )
    run_id = _seed_run(owner_conn, seeded_org["org_pk"], seeded_deal)

    async def fake_enqueue(
        storage_key: str,
        *,
        entity: str,
        known_sha256s=None,
        sector_options=None,
        geo_options=None,
        screen_criteria=None,
    ) -> str:
        assert known_sha256s is None  # D12: never the document's own fingerprint
        assert entity == "Job Test Deal"  # deal.name, per point 3
        return "job-key-1"

    async def fake_get_job(job_key: str) -> _FakeSaqJob:
        return _FakeSaqJob(Status.COMPLETE, {"status": "parsed", "bucket": "b", "key": "k"})

    monkeypatch.setattr(job_module, "enqueue_process_document_job", fake_enqueue)
    monkeypatch.setattr(job_module, "get_parse_job", fake_get_job)

    await job_module.start_deal_analysis(
        {}, analysis_run_id=run_id, deal_id=seeded_deal, clerk_org_id=seeded_org["clerk_org_id"]
    )

    run = _fetch_run(owner_conn, run_id)
    assert run["status"] == "successful"
    assert run["error_message"] is None
    assert len(run["parse_jobs"]) == 1
    assert run["parse_jobs"][0]["outcome"] == "parsed"
    assert run["parse_jobs"][0]["data_source_id"] == data_source_id
    assert run["ended_at"] is not None
    assert run["ended_at"] >= run["started_at"]
    assert run["job_comments"] == [
        {
            "dataSourceId": data_source_id,
            "fileName": "file.pdf",
            "status": "parsed",
            "comment": "Parsed successfully.",
        }
    ]

    # Point 4: chains straight into a job_name="verification" row + enqueue --
    # no job_name="extraction" row, extraction already happened above.
    assert _count_analysis_runs(owner_conn, seeded_deal, "verification") == 1
    assert _count_analysis_runs(owner_conn, seeded_deal, "extraction") == 0
    assert len(mocked_verification_enqueue) == 1
    job_name, kwargs = mocked_verification_enqueue[0]
    assert job_name == "start_deal_verification"
    assert kwargs["parsing_run_id"] == run_id
    assert kwargs["clerk_org_id"] == seeded_org["clerk_org_id"]
    assert kwargs["timeout"] == 7200


async def test_enqueue_receives_the_orgs_approved_mandate_options(
    owner_conn, seeded_org, seeded_deal, monkeypatch, mocked_verification_enqueue
):
    """Path B: the org's approved sectors/geographies AND the selected qualitative
    (llm) rules' questions are loaded once and passed to the parser -- so it can
    classify sector/HQ and search the document for the analyst's selected
    qualitative criteria."""
    from app.services.screening.workspace_config import WorkspaceConfig

    _seed_verified_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "org/a.pdf")
    run_id = _seed_run(owner_conn, seeded_org["org_pk"], seeded_deal)

    async def fake_load_config(session):
        return WorkspaceConfig(
            approved_sectors=["Fintech"],
            approved_geographies=["Canada", "United States"],
            # A must-have that maps to an llm (document-searched) rule.
            must_have_options=["Founder(s) full-time on the business"],
        )

    captured: dict[str, Any] = {}

    async def fake_enqueue(
        storage_key: str,
        *,
        entity: str,
        known_sha256s=None,
        sector_options=None,
        geo_options=None,
        screen_criteria=None,
    ) -> str:
        captured["sector_options"] = sector_options
        captured["geo_options"] = geo_options
        captured["screen_criteria"] = screen_criteria
        return "job-key-1"

    async def fake_get_job(job_key: str) -> _FakeSaqJob:
        return _FakeSaqJob(Status.COMPLETE, {"status": "parsed", "bucket": "b", "key": "k"})

    monkeypatch.setattr(job_module, "load_workspace_config", fake_load_config)
    monkeypatch.setattr(job_module, "enqueue_process_document_job", fake_enqueue)
    monkeypatch.setattr(job_module, "get_parse_job", fake_get_job)

    await job_module.start_deal_analysis(
        {}, analysis_run_id=run_id, deal_id=seeded_deal, clerk_org_id=seeded_org["clerk_org_id"]
    )

    assert captured["sector_options"] == ["Fintech"]
    assert captured["geo_options"] == ["Canada", "United States"]
    # gs_01 was selected (must-have) and is an llm rule -> its question is searched.
    assert captured["screen_criteria"] == [
        {"rule_id": "gs_01", "question": "Founder(s) full-time on the business"}
    ]


async def test_all_documents_rejected_no_extractable_text_marks_ocr_needed_and_run_failed(
    owner_conn, seeded_org, seeded_deal, monkeypatch, mocked_verification_enqueue
):
    """D15 + SIM-350 Option A: a run with zero successful parses is `failed`
    with a specific message, and the rejected document's data_source.status
    flips verified -> ocr_needed. A failed run must NOT chain into
    verification -- there's nothing to verify."""
    data_source_id = _seed_verified_data_source(
        owner_conn, seeded_org["org_pk"], seeded_deal, "org/scan.pdf"
    )
    run_id = _seed_run(owner_conn, seeded_org["org_pk"], seeded_deal)

    async def fake_enqueue(
        storage_key: str,
        *,
        entity: str,
        known_sha256s=None,
        sector_options=None,
        geo_options=None,
        screen_criteria=None,
    ) -> str:
        return "job-key-2"

    # Real message from Simpero_Gov_AI_Services' docling_parser.py:461 --
    # ParseError("no_extractable_text", "PDF contains no extractable text.", 422).
    _PARSER_MESSAGE = "PDF contains no extractable text."

    async def fake_get_job(job_key: str) -> _FakeSaqJob:
        return _FakeSaqJob(
            Status.COMPLETE,
            {"status": "rejected", "code": "no_extractable_text", "message": _PARSER_MESSAGE},
        )

    monkeypatch.setattr(job_module, "enqueue_process_document_job", fake_enqueue)
    monkeypatch.setattr(job_module, "get_parse_job", fake_get_job)

    await job_module.start_deal_analysis(
        {}, analysis_run_id=run_id, deal_id=seeded_deal, clerk_org_id=seeded_org["clerk_org_id"]
    )

    run = _fetch_run(owner_conn, run_id)
    assert run["status"] == "failed"
    assert "OCR" in run["error_message"]
    assert run["ended_at"] is not None  # ended_at stamps on failed, same as successful
    assert _fetch_data_source_status(owner_conn, data_source_id) == "ocr_needed"
    # The comment is the parser's own message, verbatim -- not this app's
    # invented wording.
    assert run["job_comments"] == [
        {
            "dataSourceId": data_source_id,
            "fileName": "file.pdf",
            "status": "rejected",
            "comment": _PARSER_MESSAGE,
        }
    ]
    assert _count_analysis_runs(owner_conn, seeded_deal, "verification") == 0
    assert not mocked_verification_enqueue


async def test_mixed_outcomes_mark_run_successful_not_failed(
    owner_conn, seeded_org, seeded_deal, monkeypatch, mocked_verification_enqueue
):
    _seed_verified_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "org/good.pdf")
    _seed_verified_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "org/scan.pdf")
    run_id = _seed_run(owner_conn, seeded_org["org_pk"], seeded_deal)

    results_by_key: dict[str, _FakeSaqJob] = {}
    counter = {"n": 0}

    async def fake_enqueue(
        storage_key: str,
        *,
        entity: str,
        known_sha256s=None,
        sector_options=None,
        geo_options=None,
        screen_criteria=None,
    ) -> str:
        counter["n"] += 1
        key = f"job-key-{counter['n']}"
        if "scan" in storage_key:
            results_by_key[key] = _FakeSaqJob(
                Status.COMPLETE, {"status": "rejected", "code": "no_extractable_text"}
            )
        else:
            results_by_key[key] = _FakeSaqJob(Status.COMPLETE, {"status": "parsed"})
        return key

    async def fake_get_job(job_key: str) -> _FakeSaqJob:
        return results_by_key[job_key]

    monkeypatch.setattr(job_module, "enqueue_process_document_job", fake_enqueue)
    monkeypatch.setattr(job_module, "get_parse_job", fake_get_job)

    await job_module.start_deal_analysis(
        {}, analysis_run_id=run_id, deal_id=seeded_deal, clerk_org_id=seeded_org["clerk_org_id"]
    )

    run = _fetch_run(owner_conn, run_id)
    assert run["status"] == "successful"  # D15: mixed outcomes -> successful, not failed
    assert run["error_message"] is None
    assert len(mocked_verification_enqueue) == 1


async def test_saq_level_job_failure_falls_back_to_generic_comment(
    owner_conn, seeded_org, seeded_deal, monkeypatch, mocked_verification_enqueue
):
    """A SAQ-level FAILED/ABORTED job never went through the parser's own
    ParseError path, so there's no `message` to use verbatim -- the only
    case where this app's own generic wording is the right call."""
    data_source_id = _seed_verified_data_source(
        owner_conn, seeded_org["org_pk"], seeded_deal, "org/broken.pdf"
    )
    run_id = _seed_run(owner_conn, seeded_org["org_pk"], seeded_deal)

    async def fake_enqueue(
        storage_key: str,
        *,
        entity: str,
        known_sha256s=None,
        sector_options=None,
        geo_options=None,
        screen_criteria=None,
    ) -> str:
        return "job-key-broken"

    async def fake_get_job(job_key: str) -> _FakeSaqJob:
        return _FakeSaqJob(Status.FAILED, None)

    monkeypatch.setattr(job_module, "enqueue_process_document_job", fake_enqueue)
    monkeypatch.setattr(job_module, "get_parse_job", fake_get_job)

    await job_module.start_deal_analysis(
        {}, analysis_run_id=run_id, deal_id=seeded_deal, clerk_org_id=seeded_org["clerk_org_id"]
    )

    run = _fetch_run(owner_conn, run_id)
    assert run["status"] == "failed"
    assert run["job_comments"] == [
        {
            "dataSourceId": data_source_id,
            "fileName": "file.pdf",
            "status": "rejected",
            "comment": "Parsing job failed unexpectedly.",
        }
    ]
    assert not mocked_verification_enqueue


async def test_missing_run_raises_instead_of_silently_no_oping(owner_conn, seeded_org, seeded_deal):
    with pytest.raises(ValueError, match="not found"):
        await job_module.start_deal_analysis(
            {},
            analysis_run_id=str(uuid.uuid4()),
            deal_id=seeded_deal,
            clerk_org_id=seeded_org["clerk_org_id"],
        )
