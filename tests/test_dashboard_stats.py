"""Unit tests for the dashboard-stats building blocks that back
deals.dashboardStats -- the DD-completion counting/percentage.

Pure, no database: compute_dd_completion_pct is arithmetic, and
_completed_deal_count takes prefetched rows and reuses the same
_deal_status_from_runs mapper the pipeline grid uses. The batched DB fetch and
the endpoint wiring are covered by test_phase1_endpoints.py.
"""

import uuid
from datetime import UTC, datetime, timedelta

from app.api.deals import _completed_deal_count
from app.models.analysis_run import AnalysisRun
from app.models.deal import Deal
from app.services.dashboard_stats import compute_dd_completion_pct

_START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _deal() -> Deal:
    return Deal(id=uuid.uuid4())


def _run(deal_id: uuid.UUID, job_name: str, status: str) -> AnalysisRun:
    return AnalysisRun(
        id=uuid.uuid4(),
        deal_id=deal_id,
        job_name=job_name,
        status=status,
        started_at=_START,
        ended_at=_START + timedelta(seconds=10) if status in ("successful", "failed") else None,
    )


# --- compute_dd_completion_pct -----------------------------------------


def test_pct_is_zero_when_no_deals():
    # 0/0 reports 0%, not a ZeroDivisionError.
    assert compute_dd_completion_pct(0, 0) == 0


def test_pct_is_zero_when_none_complete():
    assert compute_dd_completion_pct(0, 5) == 0


def test_pct_is_100_when_all_complete():
    assert compute_dd_completion_pct(3, 3) == 100


def test_pct_rounds_to_nearest_whole_percent():
    assert compute_dd_completion_pct(1, 2) == 50
    assert compute_dd_completion_pct(1, 3) == 33  # 33.33 -> 33
    assert compute_dd_completion_pct(2, 3) == 67  # 66.67 -> 67


# --- _completed_deal_count ---------------------------------------------


def test_count_is_zero_for_empty_org():
    assert _completed_deal_count([], {}, {}, {}) == 0


def test_deal_with_no_run_is_not_complete():
    deal = _deal()
    assert _completed_deal_count([deal], {}, {}, {}) == 0


def test_successful_verification_run_counts_as_complete():
    deal = _deal()
    verification = _run(deal.id, "verification", "successful")
    count = _completed_deal_count(
        [deal],
        {deal.id: verification},
        {},  # no parsing row -- the mapper tolerates it
        {deal.id: verification},
    )
    assert count == 1


def test_successful_screening_run_counts_as_complete():
    deal = _deal()
    parsing = _run(deal.id, "parsing", "successful")
    verification = _run(deal.id, "verification", "successful")
    screening = _run(deal.id, "screening", "successful")
    count = _completed_deal_count(
        [deal],
        {deal.id: screening},
        {deal.id: parsing},
        {deal.id: verification},
    )
    assert count == 1


def test_in_progress_and_failed_and_no_run_are_not_complete():
    processing = _deal()
    failed = _deal()
    none = _deal()
    parsing_wip = _run(processing.id, "parsing", "in_progress")
    verification_failed = _run(failed.id, "verification", "failed")
    count = _completed_deal_count(
        [processing, failed, none],
        {processing.id: parsing_wip, failed.id: verification_failed},
        {},
        {failed.id: verification_failed},
    )
    assert count == 0


def test_counts_only_the_completed_deals_in_a_mixed_org():
    complete = _deal()
    processing = _deal()
    no_run = _deal()
    complete_verification = _run(complete.id, "verification", "successful")
    processing_parsing = _run(processing.id, "parsing", "in_progress")
    count = _completed_deal_count(
        [complete, processing, no_run],
        {complete.id: complete_verification, processing.id: processing_parsing},
        {},
        {complete.id: complete_verification},
    )
    assert count == 1
