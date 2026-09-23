"""Unit tests for the pure `_deal_status_from_runs` mapper extracted from
`_compute_deal_status` (the N+1 batching refactor for GET /deals/pipeline).

These need no database: the mapper takes the three chain rows and returns a
DealStatusResponse, so the whole (job_name, status) decision table -- the logic
the pipeline grid and the single-deal status endpoint now share -- is verified
here in memory. The batched DB fetch itself is covered by the pipeline
integration tests (test_pipeline_intake_status / test_status_rollup)."""

import uuid
from datetime import UTC, datetime, timedelta

from app.api.deals import _deal_status_from_runs
from app.models.analysis_run import AnalysisRun
from app.services.failure_reasons import CREDIT_EXHAUSTED_CODE, CREDIT_EXHAUSTED_MESSAGE

_START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _run(
    job_name: str,
    status: str,
    *,
    started_at: datetime = _START,
    ended_at: datetime | None = None,
    error_message: str | None = None,
    job_comments: list | None = None,
) -> AnalysisRun:
    return AnalysisRun(
        id=uuid.uuid4(),
        deal_id=uuid.uuid4(),
        job_name=job_name,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        error_message=error_message,
        job_comments=job_comments,
    )


def test_parsing_queued_is_queued_with_no_phase():
    resp = _deal_status_from_runs(_run("parsing", "queued"), None, None)
    assert resp.job_status == "queued"
    assert resp.current_phase is None


def test_parsing_in_progress_is_processing_at_parsing():
    resp = _deal_status_from_runs(_run("parsing", "in_progress"), None, None)
    assert resp.job_status == "processing"
    assert resp.current_phase == "parsing"


def test_parsing_successful_advances_to_verification_with_duration():
    run = _run("parsing", "successful", ended_at=_START + timedelta(seconds=42))
    resp = _deal_status_from_runs(run, None, None)
    assert resp.job_status == "processing"
    assert resp.current_phase == "verification"
    assert resp.step_durations == {"parsing": 42}
    assert resp.ended_at == run.ended_at


def test_parsing_failed_is_error_and_passes_the_message_through():
    run = _run("parsing", "failed", ended_at=_START + timedelta(seconds=5), error_message="boom")
    resp = _deal_status_from_runs(run, None, None)
    assert resp.job_status == "error"
    assert resp.current_phase == "parsing"
    assert resp.error_message == "boom"
    # A generic (non-sentinel) failure carries no machine-readable code.
    assert resp.error_code is None


def test_parsing_failed_credit_sentinel_sets_error_code():
    # The credit sentinel error_message maps to the stable llm_credit_exhausted
    # code the frontend renders a billing-specific message + CTA for, while the
    # human-readable message is still passed through unchanged.
    run = _run(
        "parsing",
        "failed",
        ended_at=_START + timedelta(seconds=5),
        error_message=CREDIT_EXHAUSTED_MESSAGE,
    )
    resp = _deal_status_from_runs(run, None, None)
    assert resp.job_status == "error"
    assert resp.error_code == CREDIT_EXHAUSTED_CODE
    assert resp.error_message == CREDIT_EXHAUSTED_MESSAGE


def test_verification_failed_credit_sentinel_sets_error_code():
    parsing = _run("parsing", "successful", ended_at=_START + timedelta(seconds=30))
    verification = _run(
        "verification",
        "failed",
        started_at=_START + timedelta(minutes=1),
        ended_at=_START + timedelta(minutes=1, seconds=3),
        error_message=CREDIT_EXHAUSTED_MESSAGE,
    )
    resp = _deal_status_from_runs(verification, parsing, verification)
    assert resp.job_status == "error"
    assert resp.error_code == CREDIT_EXHAUSTED_CODE


def test_screening_failed_credit_sentinel_sets_error_code():
    parsing = _run("parsing", "successful", ended_at=_START + timedelta(seconds=30))
    verification = _run(
        "verification",
        "successful",
        started_at=_START + timedelta(minutes=1),
        ended_at=_START + timedelta(minutes=1, seconds=20),
    )
    screening = _run(
        "screening",
        "failed",
        started_at=_START + timedelta(minutes=2),
        ended_at=_START + timedelta(minutes=2, seconds=5),
        error_message=CREDIT_EXHAUSTED_MESSAGE,
    )
    resp = _deal_status_from_runs(screening, parsing, verification)
    assert resp.job_status == "error"
    assert resp.error_code == CREDIT_EXHAUSTED_CODE


def test_verification_successful_is_complete_at_governance():
    # The chain start comes from the PARSING row, not the verification row.
    parsing = _run("parsing", "successful", ended_at=_START + timedelta(seconds=30))
    verification = _run(
        "verification",
        "successful",
        started_at=_START + timedelta(minutes=1),
        ended_at=_START + timedelta(minutes=1, seconds=20),
    )
    resp = _deal_status_from_runs(verification, parsing, verification)
    assert resp.job_status == "complete"
    assert resp.current_phase == "governance"
    assert resp.started_at == parsing.started_at
    assert resp.step_durations == {"parsing": 30, "verification": 20}


def test_verification_in_progress_is_processing():
    parsing = _run("parsing", "successful", ended_at=_START + timedelta(seconds=30))
    verification = _run("verification", "in_progress", started_at=_START + timedelta(minutes=1))
    resp = _deal_status_from_runs(verification, parsing, verification)
    assert resp.job_status == "processing"
    assert resp.current_phase == "verification"


def test_verification_failed_is_error():
    parsing = _run("parsing", "successful", ended_at=_START + timedelta(seconds=30))
    verification = _run(
        "verification",
        "failed",
        started_at=_START + timedelta(minutes=1),
        ended_at=_START + timedelta(minutes=1, seconds=3),
        error_message="verify blew up",
    )
    resp = _deal_status_from_runs(verification, parsing, verification)
    assert resp.job_status == "error"
    assert resp.current_phase == "verification"
    assert resp.error_message == "verify blew up"


def test_screening_successful_is_complete_and_uses_verification_comments():
    # SIM-404: a successful screening row is the finished state, and job_comments
    # are read from the VERIFICATION row (screening has no per-document comments).
    parsing = _run("parsing", "successful", ended_at=_START + timedelta(seconds=30))
    verification = _run(
        "verification",
        "successful",
        started_at=_START + timedelta(minutes=1),
        ended_at=_START + timedelta(minutes=1, seconds=20),
        job_comments=[
            {"dataSourceId": "x", "fileName": "cim.pdf", "status": "verified", "comment": "ok"}
        ],
    )
    screening = _run(
        "screening",
        "successful",
        started_at=_START + timedelta(minutes=2),
        ended_at=_START + timedelta(minutes=2, seconds=5),
    )
    resp = _deal_status_from_runs(screening, parsing, verification)
    assert resp.job_status == "complete"
    assert resp.current_phase == "governance"
    # The comments come from the VERIFICATION row (screening carries none), so a
    # non-empty list here proves they were not taken from the screening run.
    assert resp.job_comments is not None
    assert [c.file_name for c in resp.job_comments] == ["cim.pdf"]
    # Screening's own elapsed time is NOT filed under verification's duration.
    assert resp.step_durations == {"parsing": 30, "verification": 20}


def test_screening_in_progress_is_processing_at_the_analysis_step():
    # Corroboration + screening run after verification as the third step; while
    # the screening row is in flight the deal is still processing, on "analysis".
    parsing = _run("parsing", "successful", ended_at=_START + timedelta(seconds=30))
    verification = _run(
        "verification",
        "successful",
        started_at=_START + timedelta(minutes=1),
        ended_at=_START + timedelta(minutes=1, seconds=20),
    )
    screening = _run("screening", "in_progress", started_at=_START + timedelta(minutes=2))
    resp = _deal_status_from_runs(screening, parsing, verification)
    assert resp.job_status == "processing"
    assert resp.current_phase == "analysis"
    assert {s.phase: s.status for s in resp.steps} == {
        "parsing": "done",
        "verification": "done",
        "analysis": "current",
    }


def test_screening_failed_is_error_at_the_analysis_step():
    parsing = _run("parsing", "successful", ended_at=_START + timedelta(seconds=30))
    verification = _run(
        "verification",
        "successful",
        started_at=_START + timedelta(minutes=1),
        ended_at=_START + timedelta(minutes=1, seconds=20),
    )
    screening = _run(
        "screening",
        "failed",
        started_at=_START + timedelta(minutes=2),
        ended_at=_START + timedelta(minutes=2, seconds=5),
        error_message="screening failed",
    )
    resp = _deal_status_from_runs(screening, parsing, verification)
    assert resp.job_status == "error"
    assert resp.current_phase == "analysis"
    assert resp.error_message == "screening failed"
    assert next(s.status for s in resp.steps if s.phase == "analysis") == "failed"
