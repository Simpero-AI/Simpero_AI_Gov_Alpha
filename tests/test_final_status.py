"""Unit tests for _final_status's credit-exhaustion branch (no database).

_final_status turns a run's per-document parse outcomes into (run_status,
error_message). The credit branch makes an AI-provider billing/quota block
(the parser's anthropic_credit_exhausted rejection) surface the actionable
sentinel instead of the generic "couldn't be parsed", so the status API can map
it to a machine-readable error_code.
"""

from app.jobs.tasks.start_deal_analysis import (
    _build_job_comments,
    _final_status,
    _job_status,
)
from app.services.failure_reasons import CREDIT_EXHAUSTED_MESSAGE, PARSER_CREDIT_REJECTION_CODE


def _job(outcome: str, code: str | None) -> dict:
    return {"outcome": outcome, "code": code}


def test_final_status_credit_rejection_surfaces_the_actionable_sentinel():
    status, message = _final_status(
        [_job("rejected", PARSER_CREDIT_REJECTION_CODE)], timed_out=False
    )
    assert status == "failed"
    assert message == CREDIT_EXHAUSTED_MESSAGE


def test_final_status_credit_wins_over_other_rejections_when_mixed():
    # An account-level block fails every call, so a single credit rejection in a
    # mixed batch still points at the actionable cause, not "needs OCR"/"couldn't
    # parse".
    jobs = [
        _job("rejected", "no_extractable_text"),
        _job("rejected", PARSER_CREDIT_REJECTION_CODE),
    ]
    _, message = _final_status(jobs, timed_out=False)
    assert message == CREDIT_EXHAUSTED_MESSAGE


def test_final_status_a_successful_parse_is_not_credit_failed():
    # The credit branch only applies when there are zero successful parses.
    jobs = [_job("parsed", None), _job("rejected", PARSER_CREDIT_REJECTION_CODE)]
    status, message = _final_status(jobs, timed_out=False)
    assert status == "successful"
    assert message is None


def test_final_status_generic_rejection_message_is_unchanged():
    status, message = _final_status([_job("rejected", "job_failed")], timed_out=False)
    assert status == "failed"
    assert message == "None of this deal's documents could be parsed."


def test_job_status_credit_rejection_is_paused_not_rejected():
    # A credit/usage-limit block is an account issue, not a bad document: the
    # per-doc status the UI renders must read `paused`, not the parser's raw
    # `rejected`, so it lines up with the amber "Analysis paused" banner.
    assert _job_status(_job("rejected", PARSER_CREDIT_REJECTION_CODE)) == "paused"


def test_job_status_other_outcomes_keep_the_parser_vocabulary():
    assert _job_status(_job("parsed", None)) == "parsed"
    assert _job_status(_job("rejected", "no_extractable_text")) == "rejected"
    assert _job_status(_job("rejected", "job_failed")) == "rejected"
    assert _job_status({"outcome": None, "code": None}) == "pending"


def test_build_job_comments_relabels_a_credit_blocked_document_as_paused():
    # End to end through the findings summary the frontend reads off
    # GET .../status: a healthy document blocked only by the account's credit
    # cap is reported `paused`, never `rejected`.
    jobs = [
        {
            "data_source_id": "ds-1",
            "filename": "cim.pdf",
            "outcome": "rejected",
            "code": PARSER_CREDIT_REJECTION_CODE,
            "message": CREDIT_EXHAUSTED_MESSAGE,
        }
    ]
    comments = _build_job_comments(jobs, timed_out=False)
    assert comments[0]["status"] == "paused"
    assert comments[0]["dataSourceId"] == "ds-1"
    assert comments[0]["fileName"] == "cim.pdf"
