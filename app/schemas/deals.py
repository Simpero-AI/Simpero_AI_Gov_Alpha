from datetime import datetime
from typing import Literal

from pydantic import Field

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


# The three states the Live Pipeline grid routes on (P3-06, F4/D3), NOT the
# four deal_intake_link statuses. Anything that is not live-and-waiting or
# actually-submitted -- no link, revoked, expired, or still stored `pending`
# past its expires_at -- collapses to "none" and routes exactly like a deal
# that never had a link. See compute_pipeline_intake_status.
IntakePipelineStatus = Literal["none", "pending", "submitted"]
# Cross-repo contract, enforced by nothing but this comment: the Web half is
# `LivePipelineRow` in Simpero_AI_Gov_Web/src/shared/dealsListPipeline.ts,
# whose member list must stay character-identical to this Literal. As of this
# change that type stops at `agentStatus` and has no `intakeStatus` at all --
# on `staging` and on the P4/P5 branch alike -- even though
# Simpero_AI_Gov_Web/src/api/intakeLink.ts already documents the consumer
# side ("Callers only enable this query once `intakeStatus === 'submitted'`").
# Adding a field to a JSON response is non-breaking under TypeScript's
# structural typing, so nothing in Web breaks when this lands; but P5-07's
# conditional grid routing cannot be written until that shared type gains
# `intakeStatus: "none" | "pending" | "submitted";`. Tracked as a Web-side
# follow-up, not a blocker here.


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
    intake_status: IntakePipelineStatus


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
    sector: str | None
    hq_geography: str | None
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


# Mirrors DataSource._STATUSES (app/models/data_source.py) -- exported so
# the route can `cast` the ORM column's plain `str` into this type at
# construction time, since the DB CHECK constraint already guarantees it,
# not just the Pydantic model that types the field.
DealDocumentStatus = Literal["pending", "verified", "quarantined", "ocr_needed", "mismatch"]


class DealDocumentResponse(CamelModel):
    """One data_source row (P3-04). No field here distinguishes an org-side
    upload from an external-intake upload (P3-10) -- both write through the
    same DataSourceRepo, so the list is uniform by construction, not by a
    filter applied here."""

    id: str
    filename: str
    # A Literal here just echoes an already DB-constrained column back out
    # (this endpoint never writes it), but makes the OpenAPI schema
    # self-documenting for whoever builds the frontend's status list
    # (Step 3, P5-05) instead of them having to go find _STATUSES.
    status: DealDocumentStatus
    created_at: datetime


class CreateDealRequest(CamelModel):
    name: str
    gp_source: str | None
    deal_size_min_usd: int | None
    deal_size_max_usd: int | None
    sector_tags: list[str] | None
    sector: str | None = None
    hq_geography: str | None = None


class CreateDealResponse(CamelModel):
    id: str


class UpdateDealRequest(CamelModel):
    """deals.update -- PATCH /deals/{id}. All fields optional and unset by
    default: `model_dump(exclude_unset=True)` on the route only applies keys
    the client actually included in the JSON body, so a field entirely
    omitted stays untouched while an explicit `null` clears it. Lets a human
    fill in sector/hq_geography on a legacy deal."""

    sector: str | None = None
    hq_geography: str | None = None


class StartAnalysisRequest(CamelModel):
    """Persisted verbatim onto analysis_run.selected_frameworks, not
    interpreted — nothing consumes it yet (Open Question 3)."""

    selected_frameworks: list[str] | None = None


class ScreeningRuleResultResponse(CamelModel):
    """One rule's verdict as it appears in screening_result.rule_results.

    Mirrors RuleResult.to_json() (app/services/screening/types.py) rather
    than re-deriving it: the stored shape and the wire shape are deliberately
    the same object, so a reviewer reading the API response is reading
    exactly what was written to the provenance record. The two exceptions --
    `question` and `kind` -- are joined from the rulebook at read time (see
    below), not stored on the row.

    `evidence_ref` stays a bare dict — it is a discriminated union
    (`kind: "claim" | "deal_field"`) whose arms have different fields, and
    flattening it into one optional-everything model would lose precisely the
    distinction it exists to carry.
    """

    rule_id: str
    verdict: str
    evaluator: str
    evidence_ref: dict | None
    confidence: float
    reason: str | None
    # Joined from the rulebook by rule_id at read time, NOT part of the stored
    # RuleResult: track_b.yaml is the single source of truth for a rule's
    # question text and kind, so the frontend renders the human-readable
    # question and tells a met green_signal from a tripped deal_breaker without
    # duplicating policy. Null if the stored rule_id is absent from the current
    # rulebook (version skew).
    question: str | None = None
    kind: str | None = None


class ScreeningResultResponse(CamelModel):
    """GET /deals/{id}/screening — the deal's most recent screening pass.

    A recommendation, not a decision: `auto_decline` means a deal-breaker
    matched and cites which, but nothing in the system declines the deal on
    its own. On an `auto_decline` the engine short-circuits, so `ruleResults`
    is a partial list ending at the breaker that fired — that is the real
    record of what ran, not a truncation to paper over.
    """

    id: str
    deal_id: str
    analysis_run_id: str | None
    rulebook_version: str
    recommendation: str
    rule_results: list[ScreeningRuleResultResponse]
    created_at: datetime


class ScreeningCitedFieldResponse(CamelModel):
    """One extracted fact for the Initial Screening "Extracted from Materials"
    panel: a labelled value copied verbatim from a claim, with a human citation
    string (document · location) so the reviewer can see where it came from."""

    label: str
    value: str
    citation: str | None = None


class ScreeningMaterialsResponse(CamelModel):
    """GET /deals/{id}/screening-materials — the deal's key extracted facts,
    derived deterministically from the claims spine (build_screening_materials):
    each canonical metric's latest actual figure, straight from the verified
    claims. Deliberately claims-only and LLM-free, so this response is fast and
    reliable and never blocked by the insights model call -- the LLM-derived
    Agent Highlights / Risk Flags live on their own endpoint
    (GET /deals/{id}/screening-insights) so a slow or failed model call can
    never take the extracted panel down with it."""

    extracted_fields: list[ScreeningCitedFieldResponse]


class ScreeningInsightsResponse(CamelModel):
    """GET /deals/{id}/screening-insights — the LLM-derived Agent Highlights
    (positive signals) and Risk Flags (concerns) for the deal
    (derive_screening_insights), over the same trusted claims the extracted panel
    shows. Its own endpoint, decoupled from the extracted facts on purpose.
    `highlights`/`riskFlags` come back empty when the pass is unavailable (no
    key) or fails soft (model/transport error, timeout) -- the panels then render
    their own empty state."""

    highlights: list[str]
    risk_flags: list[str]


class MarketFactResponse(CamelModel):
    """One Market-tab fact copied from the claims spine (build_market_view): a
    market-size figure's formatted value, or a qualitative market/competition
    assertion verbatim. `label` is the metric name (e.g. "TAM") for sizing, or
    the entity the assertion is about for a qualitative fact; `status` is the
    trust status (verified/partially_verified/cited) so the tab can badge it;
    `citation` is the human "file · p.N" string, null when unlocatable."""

    label: str
    value: str
    citation: str | None = None
    status: str
    entity: str | None = None


class CompanyFactResponse(CamelModel):
    """One Business Overview fact copied from the claims spine (build_company_view):
    a company-identity value (sector/HQ/headcount/founded) or a qualitative
    assertion verbatim. `label` is the field name for a fact, or the entity the
    assertion is about; `status` is the trust status ("verified"/"cited"/... , or
    "derived" for the deal-profile sector/HQ) so the tab can badge it; `citation`
    is the human "file · p.N" string, null when unlocatable (or derived)."""

    label: str
    value: str
    citation: str | None = None
    status: str
    entity: str | None = None


class MarketViewResponse(CamelModel):
    """GET /deals/{id}/market — the Market tab's claims-driven content: numeric
    market sizing recovered by label, plus the qualitative market-definition and
    competitive-position assertions. Each list is empty when the deal has no
    backing claims (the tab then renders "information not available"); never
    404s for a claim-less deal. Corroboration/search enrichment is a separate
    track (the corroboration engine and web search are not yet producing data)."""

    sizing: list[MarketFactResponse]
    market_definition: list[MarketFactResponse]
    competitive_position: list[MarketFactResponse]


class CompanyViewResponse(CamelModel):
    """GET /deals/{id}/company — the Business Overview tab's claims-driven content:
    company-identity facts (sector/HQ from the deal profile, headcount/founded by
    label), plus qualitative assertions grouped by kind -- overview
    (operating_model), risks (risk_or_dependency), commercial (commercial_terms),
    related_parties (related_party), plans (plan_or_commitment). Each list is
    empty when the deal has no backing claims (the tab renders "information not
    available"); never 404s for a claim-less deal."""

    facts: list[CompanyFactResponse]
    overview: list[CompanyFactResponse]
    risks: list[CompanyFactResponse]
    commercial: list[CompanyFactResponse]
    related_parties: list[CompanyFactResponse]
    plans: list[CompanyFactResponse]


class FormerNameResponse(CamelModel):
    """A previous legal name with the window it applied to.

    The dates are the point, not decoration: they are what lets a reader tell
    "this document is old and uses the old name" from "this is a different
    company". Both bounds are optional — EDGAR omits `to` on an open range and
    has thin history for older filers, and an absent date is unknown, never
    inferred.
    """

    name: str
    # `from` is a Python keyword, so the attribute is `from_` with an explicit
    # alias. to_camel would leave the trailing underscore on the wire, and the
    # stored JSONB (and EDGAR itself) spells it `from` — the alias keeps the
    # persisted shape and the wire shape identical, as everywhere else here.
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None


class EntityResolutionResponse(CamelModel):
    """GET/POST /deals/{id}/entity-resolution — which real-world company this
    deal was resolved to, and on what evidence.

    Three outcomes, and the last two are NOT the same thing:
    `notFound` means we searched and this company genuinely has no SEC filer —
    the expected answer for a private target, and never a negative finding
    about the deal. `unresolved` means we could not tell (ambiguous name, or
    SEC's own endpoints disagreeing) and therefore checked nothing.

    `registryId` (EDGAR's 10-digit CIK) is present only on `resolved`, and
    always present on it — the database enforces both directions.
    """

    id: str
    deal_id: str
    source: str
    status: Literal["resolved", "not_found", "unresolved"]
    query_name: str
    registry_id: str | None
    legal_name: str | None
    former_names: list[FormerNameResponse]
    matched_on: Literal["current_name", "former_name"] | None
    reason: str | None
    # Which endpoints were called, candidate counts, the matched name. A bare
    # dict for the same reason as ScreeningRuleResultResponse.evidence_ref:
    # its keys legitimately differ per outcome, and flattening it into one
    # optional-everything model would lose that.
    evidence: dict
    created_at: datetime
