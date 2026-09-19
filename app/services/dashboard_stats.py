"""Backs deals.dashboardStats. computeWindowBounds/computePipelineValueDelta
are declared type-only (no implementation body) in the frozen contract
(src/api/_legacy/server/dashboardStats.d.ts) — the actual monorepo logic
isn't available to port exactly, so this is a best-effort read against the
same shape, documented inline. Fixed to the "month" window; Phase 1 has no
UI window selector wired to this endpoint yet.
"""

from datetime import UTC, datetime
from typing import Literal

PipelineValueDelta = int | Literal["new"] | None


def compute_month_bounds(now: datetime | None = None) -> tuple[datetime, datetime, datetime]:
    """Returns (current_start, prior_start, prior_end==current_start)."""
    now = now or datetime.now(UTC)
    current_start = now.replace(hour=0, minute=0, second=0, microsecond=0, day=1)
    if current_start.month == 1:
        prior_start = current_start.replace(year=current_start.year - 1, month=12)
    else:
        prior_start = current_start.replace(month=current_start.month - 1)
    return current_start, prior_start, current_start


def compute_pipeline_value_delta(current: int, prior: int) -> PipelineValueDelta:
    """ "new" when there was nothing in the prior window and there is now —
    a percentage/delta against zero isn't meaningful. None when both are
    zero (nothing to report either way)."""
    if prior == 0:
        return "new" if current > 0 else None
    return current - prior


def compute_dd_completion_pct(complete: int, total: int) -> int:
    """Whole-number percentage of deals whose analysis chain reached the
    terminal "complete" state. 0 when there are no deals — 0/0 reports as 0%,
    the honest "nothing completed" reading, not undefined. The caller counts
    `complete` with the same `_deal_status_from_runs` mapper the pipeline grid
    and status endpoint use, so this stays a pure round()."""
    if total <= 0:
        return 0
    return round(complete / total * 100)
