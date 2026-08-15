from datetime import datetime
from typing import Literal

from app.schemas.common import CamelModel


class PipelineStepResponse(CamelModel):
    phase: str
    title: str
    detail: str
    status: Literal["done", "current", "pending", "failed"]


class JobCommentResponse(CamelModel):
    """One `analysis_run.job_comments` entry — a frontend-facing summary of
    what happened to one document, derived from `parse_jobs` once the run
    goes terminal (see app/jobs/tasks/start_deal_analysis.py::_build_job_comments)."""

    data_source_id: str
    file_name: str | None
    status: str
    comment: str


class DealStatusResponse(CamelModel):
    """`deals.status` / DealStatusPayload. Phase 1 has no job model yet, so
    this is always the `no_job` shape — Phase 2's job model fills in real
    jobStatus/currentPhase/steps.
    """

    job_status: Literal["queued", "processing", "complete", "error", "no_job"]
    current_phase: str | None
    steps: list[PipelineStepResponse]
    # started_at is the CHAIN's start (the parsing run's own started_at, even
    # while current_phase has since moved on to "verification"/"governance" and a
    # different (verification) row is now the "latest" one) -- not simply
    # "the latest run's own started_at" -- so the frontend's elapsed timer
    # reads "since analysis began," not "since the current stage began."
    # Null for the no_job shape (no run exists yet).
    started_at: datetime | None = None
    # The latest run's own ended_at -- null while it's still queued/running.
    # Lets the frontend freeze the elapsed timer at a real value instead of
    # ticking forever once nothing further will happen.
    ended_at: datetime | None = None
    # Real per-step wall time in seconds, keyed by the same phase strings as
    # `steps` ("parsing"/"verification") -- present only once that step's own run
    # has a real ended_at, i.e. only for a step that's actually finished.
    step_durations: dict[str, int] = {}
    error_message: str | None = None
    # Only populated once a run reaches a terminal status (successful/failed)
    # -- null everywhere else, including no_job/queued/processing.
    job_comments: list[JobCommentResponse] | None = None


class ValueDelta(CamelModel):
    value: int
    delta: int


class PipelineValueStat(CamelModel):
    value: int
    delta: int | Literal["new"] | None


class AvgAiScoreStat(CamelModel):
    value: float | None
    delta: float | None


class DdCompletionStat(CamelModel):
    value: int
    delta_pp: int


class DashboardStatsResponse(CamelModel):
    """`deals.dashboardStats` / DashboardStatsPayload.

    computeWindowBounds/computePipelineValueDelta are declared (types only,
    no implementation body) in the frozen contract — the actual monorepo
    logic isn't available to port exactly. This is a best-effort read
    against the same shape: current-vs-prior calendar-month counts/sums,
    "new" when prior is 0 and current isn't. avgAiScore/ddCompletionPct are
    null/0 in Phase 1 — there's no scoring writer until the real pipeline.
    """

    window: Literal["week", "month", "quarter"]
    total_deals: ValueDelta
    pipeline_value_usd: PipelineValueStat
    avg_ai_score: AvgAiScoreStat
    dd_completion_pct: DdCompletionStat


class LivePipelineRowResponse(CamelModel):
    deal_id: str
    name: str
    gp_source: str
    sector_tags: list[str]
    state: str
    created_at: datetime

    valuation_usd: int | None
    ev_revenue: float | None
    ai_score: float | None
    mandate_fit_pct: float | None
    irr_pct: int | None
    action_pill: Literal["approve", "review", "decline"] | None
    metric_discrepancy_fields: list[str] | None

    agent_status: DealStatusResponse


class DealRowResponse(CamelModel):
    """The `deal` half of `DealWithLatestMemo`. No `userId` — deals are
    org-scoped in this backend, not user-scoped (deviation from the legacy
    per-user DealRowShape; see Phase 0). `deals.user_id` exists in the DB as
    creator metadata only — no query here filters on it. `sector_tags` is a
    JSON string (parseSectorTags on the frontend), matching the frozen
    contract's `DealRowShape.sectorTags: string`.
    """

    id: str
    name: str
    gp_source: str | None
    deal_size_min_usd: int | None
    deal_size_max_usd: int | None
    sector_tags: str
    state: str
    created_at: datetime
    updated_at: datetime


class LatestMemoSessionResponse(CamelModel):
    id: str
    session_id: str
    file_name: str
    memo_json: str
    created_at: datetime


class DealWithLatestMemoResponse(CamelModel):
    deal: DealRowResponse
    latest_memo_session: LatestMemoSessionResponse | None


class CreateDealRequest(CamelModel):
    name: str
    gp_source: str | None
    deal_size_min_usd: int | None
    deal_size_max_usd: int | None
    sector_tags: list[str] | None


class CreateDealResponse(CamelModel):
    id: str


class StartAnalysisRequest(CamelModel):
    """Persisted verbatim onto analysis_run.selected_frameworks, not
    interpreted — nothing consumes it yet (Open Question 3)."""

    selected_frameworks: list[str] | None = None
