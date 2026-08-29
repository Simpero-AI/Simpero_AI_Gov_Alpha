import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_claims, get_db
from app.core.intake_security import sha256_hex
from app.jobs.queue import get_queue
from app.models.analysis_run import AnalysisRun
from app.models.claim import Claim
from app.models.deal import Deal
from app.models.entity_resolution import EntityResolution
from app.repo.AnalysisRunRepo import AnalysisRunRepo
from app.repo.DataSourceRepo import DataSourceRepo
from app.repo.DealIntakeQuestionRepo import DealIntakeQuestionRepo
from app.repo.DealRepo import DealRepo
from app.repo.EntityResolutionRepo import EntityResolutionRepo
from app.repo.HumanAuditRepo import HumanAuditRepo
from app.repo.IntakeLinkRepo import IntakeLinkRepo
from app.repo.ScreeningResultRepo import ScreeningResultRepo
from app.repo.SessionRepo import SessionRepo
from app.repo.UserRepo import UserRepo
from app.schemas.deals import (
    AvgAiScoreStat,
    CreateDealRequest,
    CreateDealResponse,
    DashboardStatsResponse,
    DdCompletionStat,
    DealDocumentResponse,
    DealDocumentStatus,
    DealRowResponse,
    DealStatusResponse,
    DealWithLatestMemoResponse,
    EntityResolutionResponse,
    FormerNameResponse,
    LatestMemoSessionResponse,
    LivePipelineRowResponse,
    MarketFactResponse,
    MarketViewResponse,
    PipelineStepResponse,
    PipelineValueStat,
    ScreeningCitedFieldResponse,
    ScreeningInsightsResponse,
    ScreeningMaterialsResponse,
    ScreeningResultResponse,
    StartAnalysisRequest,
    UpdateDealRequest,
    ValueDelta,
)
from app.schemas.intake_link import CreateIntakeLinkRequest, CreateIntakeLinkResponse
from app.services.dashboard_stats import compute_month_bounds, compute_pipeline_value_delta
from app.services.entity_resolution import get_resolver
from app.services.entity_resolution.types import EntityResolutionError
from app.services.intake_links import compute_intake_link_effective_status
from app.services.market_view import build_market_view
from app.services.memo_summary import derive_pipeline_metrics
from app.services.pipeline_steps import no_job_steps
from app.services.screening.rule_view import enrich_rule_results
from app.services.screening.rulebook import load_rulebook
from app.services.screening_insights import derive_screening_insights
from app.services.screening_materials import build_screening_materials

_INTAKE_LINK_TTL_DAYS = 7

router = APIRouter(prefix="/deals", tags=["deals"])


async def _actor(db: AsyncSession, claims: dict[str, Any]) -> tuple[int, str, str | None, int]:
    """(org_id, actor_id, actor_email, user_id) -- actor_id is the Clerk id
    (audit rows), user_id is the local users.id (FK columns like
    deals.user_id)."""
    user = await UserRepo(db).get_by_clerk_id(claims["user_id"])
    assert user is not None  # get_db JIT-provisions this row before the handler runs
    return user.org_id, claims["user_id"], user.email, user.id


def _steps_for_status(
    current_phase: str | None, failed_phase: str | None = None
) -> list[PipelineStepResponse]:
    """D14 of docs/plans/start-analysis-flow-alpha.md: phases before
    `current_phase` are "done", `current_phase` itself is "current",
    `failed_phase` (if given) is "failed" and nothing after it is reachable —
    everything else is "pending".

    `current_phase` can be a value past the tracked list (currently just
    "governance", once verification succeeds) — nothing is actively running
    at that point, so it's treated as past the end: every listed step
    "done", none "current"."""
    phases = [step["phase"] for step in no_job_steps()]
    if current_phase is None:
        current_index = None
    elif current_phase in phases:
        current_index = phases.index(current_phase)
    else:
        current_index = len(phases)

    steps = []
    for index, step in enumerate(no_job_steps()):
        if failed_phase is not None:
            step_status = "failed" if step["phase"] == failed_phase else "pending"
        elif current_index is None:
            step_status = "pending"
        elif step["phase"] == current_phase:
            step_status = "current"
        elif index < current_index:
            step_status = "done"
        else:
            step_status = "pending"
        steps.append(
            PipelineStepResponse(
                phase=step["phase"], title=step["title"], detail=step["detail"], status=step_status
            )
        )
    return steps


def _run_seconds(run: AnalysisRun) -> int | None:
    """Real wall time for one analysis_run, from its own started_at/ended_at
    -- None while it hasn't ended yet (never a guess at an in-progress
    duration)."""
    if run.ended_at is None:
        return None
    return int((run.ended_at - run.started_at).total_seconds())


def _no_job_status() -> DealStatusResponse:
    """No analysis_run exists yet for this deal — every step pending."""
    return DealStatusResponse(
        job_status="no_job", current_phase=None, steps=_steps_for_status(None)
    )


def _deal_row_response(deal: Deal) -> DealRowResponse:
    return DealRowResponse(
        id=str(deal.id),
        name=deal.name,
        gp_source=deal.gp_source,
        deal_size_min_usd=deal.deal_size_min_usd,
        deal_size_max_usd=deal.deal_size_max_usd,
        # Stringified JSON, per the frozen contract's DealRowShape.sectorTags
        # (parseSectorTags on the frontend) — not the real array `listPipeline`
        # returns for the same column.
        sector_tags=json.dumps(deal.sector_tags or []),
        sector=deal.sector,
        hq_geography=deal.hq_geography,
        state=deal.status,
        created_at=deal.created_at,
        updated_at=deal.updated_at,
    )


@router.post("", response_model=CreateDealResponse, status_code=status.HTTP_201_CREATED)
async def create_deal(
    body: CreateDealRequest,
    claims: dict[str, Any] = Depends(get_claims),
    db: AsyncSession = Depends(get_db),
) -> CreateDealResponse:
    """deals.create. fund_id stays null — the frontend's contract has no fund
    field yet (see the comment on Deal.fund_id)."""
    org_id, actor_id, actor_email, user_id = await _actor(db, claims)

    deal = await DealRepo(db).create(
        {
            "id": uuid.uuid4(),
            "org_id": org_id,
            "user_id": user_id,
            "name": body.name,
            "gp_source": body.gp_source,
            "deal_size_min_usd": body.deal_size_min_usd,
            "deal_size_max_usd": body.deal_size_max_usd,
            "sector_tags": body.sector_tags,
            "sector": body.sector,
            "hq_geography": body.hq_geography,
        }
    )
    await HumanAuditRepo(db).append(
        {
            "org_id": org_id,
            "actor_id": actor_id,
            "actor_email": actor_email,
            "event_type": "deal_created",
            "deal_id": deal.id,
        }
    )

    return CreateDealResponse(id=str(deal.id))


@router.get("/pipeline", response_model=list[LivePipelineRowResponse])
async def list_pipeline(db: AsyncSession = Depends(get_db)) -> list[LivePipelineRowResponse]:
    """deals.listPipeline — Dashboard's Live Pipeline table."""
    deal_repo = DealRepo(db)
    session_repo = SessionRepo(db)
    deals = await deal_repo.list()

    rows: list[LivePipelineRowResponse] = []
    for deal in deals:
        # ponytail: one query per deal for its latest session, and one more
        # for its analysis-run status (N+1 x2) — fine at pipeline-table
        # scale; batch this (single query with a lateral join or window
        # function) if the pipeline table gets large.
        latest_session = await session_repo.latest_for_deal(deal.id)
        metrics = derive_pipeline_metrics(latest_session.memo_json if latest_session else None)
        rows.append(
            LivePipelineRowResponse(
                deal_id=str(deal.id),
                name=deal.name,
                gp_source=deal.gp_source or "",
                sector_tags=deal.sector_tags or [],
                state=deal.status,
                created_at=deal.created_at,
                agent_status=await _compute_deal_status(db, deal.id),
                **metrics,
            )
        )
    return rows


@router.get("/dashboard-stats", response_model=DashboardStatsResponse)
async def dashboard_stats(db: AsyncSession = Depends(get_db)) -> DashboardStatsResponse:
    """deals.dashboardStats. See app/services/dashboard_stats.py for the
    "best-effort read against a types-only contract" caveat."""
    current_start, prior_start, prior_end = compute_month_bounds()
    agg = await DealRepo(db).dashboard_aggregates(current_start, prior_start, prior_end)

    return DashboardStatsResponse(
        window="month",
        total_deals=ValueDelta(
            value=agg["total_deals"],
            delta=agg["current_window_deals"] - agg["prior_window_deals"],
        ),
        pipeline_value_usd=PipelineValueStat(
            value=agg["total_pipeline_value"],
            delta=compute_pipeline_value_delta(
                agg["current_window_value"], agg["prior_window_value"]
            ),
        ),
        # No scoring/completion data in Phase 1 — there's no analyse pipeline
        # writing memo_json.scoringResult yet.
        avg_ai_score=AvgAiScoreStat(value=None, delta=None),
        dd_completion_pct=DdCompletionStat(value=0, delta_pp=0),
    )


@router.get("/{deal_id}", response_model=DealWithLatestMemoResponse)
async def get_deal(
    deal_id: uuid.UUID,
    claims: dict[str, Any] = Depends(get_claims),
    db: AsyncSession = Depends(get_db),
) -> DealWithLatestMemoResponse:
    """deals.get -> DealWithLatestMemo. 404 falls out of RLS returning no
    row — not a manual ownership check."""
    deal = await DealRepo(db).get_by_id(deal_id)
    if deal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deal not found")

    org_id, actor_id, actor_email, _ = await _actor(db, claims)
    await HumanAuditRepo(db).append(
        {
            "org_id": org_id,
            "actor_id": actor_id,
            "actor_email": actor_email,
            "event_type": "document_access",
            "deal_id": deal_id,
        }
    )

    latest_session = await SessionRepo(db).latest_for_deal(deal_id)
    latest_memo_session: LatestMemoSessionResponse | None = None
    if latest_session is not None:
        latest_memo_session = LatestMemoSessionResponse(
            id=str(latest_session.id),
            session_id=str(latest_session.id),
            file_name=latest_session.file_name,
            memo_json=json.dumps(latest_session.memo_json),
            created_at=latest_session.created_at,
        )
    return DealWithLatestMemoResponse(
        deal=_deal_row_response(deal), latest_memo_session=latest_memo_session
    )


@router.patch("/{deal_id}", response_model=DealRowResponse)
async def update_deal(
    deal_id: uuid.UUID,
    body: UpdateDealRequest,
    claims: dict[str, Any] = Depends(get_claims),
    db: AsyncSession = Depends(get_db),
) -> DealRowResponse:
    """deals.update -- sets sector/hq_geography on an already-created deal.
    Needed for legacy deals with neither set. `exclude_unset=True`
    gives true partial-update semantics: a field the client omits entirely
    is left untouched, while an explicit `null` clears it."""
    deal = await DealRepo(db).get_by_id(deal_id)
    if deal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deal not found")

    updates = body.model_dump(exclude_unset=True)
    updated = await DealRepo(db).update(deal_id, updates)
    assert updated is not None  # just confirmed the row exists, above

    org_id, actor_id, actor_email, _ = await _actor(db, claims)
    await HumanAuditRepo(db).append(
        {
            "org_id": org_id,
            "actor_id": actor_id,
            "actor_email": actor_email,
            "event_type": "deal_updated",
            "deal_id": deal_id,
            "payload": {"fields": list(updates.keys())},
        }
    )

    return _deal_row_response(updated)


@router.get("/{deal_id}/screening", response_model=ScreeningResultResponse)
async def get_deal_screening(
    deal_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> ScreeningResultResponse:
    """SIM-404: the deal's most recent screening pass -- recommendation,
    rulebook version, and every rule's verdict with its evidence.

    Deliberately its own endpoint rather than a new phase on
    `GET /{deal_id}/status`: that response's `steps` list is ported from
    Simpero_AI_Gov_Web's src/shared/pipelineSteps.ts and must stay in sync
    with it (app/services/pipeline_steps.py), so adding a step there is a
    cross-repo change. It also carries a known trap -- a listed phase no job
    sets gets marked "done" once current_phase moves past its index, which
    told users stages had run that never did. Screening reads cleanly as its
    own resource, so nothing about the frontend contract has to move for it.

    404 distinguishes the two real cases in its detail: no such deal, versus
    a deal that simply has not been screened yet.
    """
    deal = await DealRepo(db).get_by_id(deal_id)
    if deal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deal not found")

    result = await ScreeningResultRepo(db).latest_for_deal(deal_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This deal has not been screened yet",
        )

    enriched_rules = enrich_rule_results(
        result.rule_results, load_rulebook(), result.rulebook_version
    )

    return ScreeningResultResponse(
        id=str(result.id),
        deal_id=str(result.deal_id),
        analysis_run_id=str(result.analysis_run_id) if result.analysis_run_id else None,
        rulebook_version=result.rulebook_version,
        recommendation=result.recommendation,
        # Stored shape == wire shape (RuleResult.to_json) plus the two joined
        # rulebook fields; validates the persisted rows rather than rebuilding
        # them field by field.
        rule_results=enriched_rules,
        created_at=result.created_at,
    )


@router.get("/{deal_id}/screening-materials", response_model=ScreeningMaterialsResponse)
async def get_deal_screening_materials(
    deal_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> ScreeningMaterialsResponse:
    """The Initial Screening tab's "Extracted from Materials" panel, derived
    deterministically from the deal's claims spine (build_screening_materials):
    each canonical metric's latest actual figure, with its citation. RLS-scoped
    by get_db.

    Claims-only and LLM-free on purpose -- fast and reliable, and never blocked
    by the insights model call (that lives on GET /{deal_id}/screening-insights).
    Does NOT require the deal to have been screened; returns empty (never 404)
    for a deal with no displayable claims, so the panel renders its own empty
    state.
    """
    deal = await DealRepo(db).get_by_id(deal_id)
    if deal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deal not found")

    claims = list((await db.execute(select(Claim).where(Claim.deal_id == deal_id))).scalars().all())
    filenames = {ds.id: ds.filename for ds in await DataSourceRepo(db).list_for_deal(deal_id)}

    materials = build_screening_materials(
        claims, dashboard_structure=deal.dashboard_structure, filenames=filenames
    )
    return ScreeningMaterialsResponse(
        extracted_fields=[
            ScreeningCitedFieldResponse(label=f.label, value=f.value, citation=f.citation)
            for f in materials.extracted_fields
        ],
    )


@router.get("/{deal_id}/screening-insights", response_model=ScreeningInsightsResponse)
async def get_deal_screening_insights(
    deal_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> ScreeningInsightsResponse:
    """The Initial Screening tab's Agent Highlights + Risk Flags -- the LLM pass
    (derive_screening_insights) over the same trusted claims the extracted panel
    shows.

    A separate endpoint from screening-materials on purpose: this call can be
    slow or fail, and isolating it means it can only ever affect these two
    panels, never the extracted facts. Fails soft to empty lists (no key, no
    claims, or any model/transport error/timeout); RLS-scoped by get_db; never
    404s for a claim-less deal.
    """
    deal = await DealRepo(db).get_by_id(deal_id)
    if deal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deal not found")

    claims = list((await db.execute(select(Claim).where(Claim.deal_id == deal_id))).scalars().all())
    highlights, risk_flags = await derive_screening_insights(
        claims, company=deal.name, dashboard_structure=deal.dashboard_structure
    )
    return ScreeningInsightsResponse(highlights=highlights, risk_flags=risk_flags)


@router.get("/{deal_id}/market", response_model=MarketViewResponse)
async def get_deal_market(
    deal_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> MarketViewResponse:
    """The Market tab, derived deterministically from the deal's claims spine
    (build_market_view): numeric market sizing (TAM/SAM/SOM/market size/CAGR)
    recovered by label, plus the qualitative market-definition and
    competitive-position assertions the parser's qualitative tier emits -- each
    with its citation and trust status. Claims-only and LLM-free; RLS-scoped by
    get_db; returns empty lists (never 404) for a deal with no market claims, so
    each panel renders its own "information not available" state."""
    deal = await DealRepo(db).get_by_id(deal_id)
    if deal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deal not found")

    claims = list((await db.execute(select(Claim).where(Claim.deal_id == deal_id))).scalars().all())
    filenames = {ds.id: ds.filename for ds in await DataSourceRepo(db).list_for_deal(deal_id)}

    view = build_market_view(claims, filenames=filenames)

    def _facts(facts: list) -> list[MarketFactResponse]:
        return [
            MarketFactResponse(
                label=f.label,
                value=f.value,
                citation=f.citation,
                status=f.status,
                entity=f.entity,
            )
            for f in facts
        ]

    return MarketViewResponse(
        sizing=_facts(view.sizing),
        market_definition=_facts(view.market_definition),
        competitive_position=_facts(view.competitive_position),
    )


async def _compute_deal_status(db: AsyncSession, deal_id: uuid.UUID) -> DealStatusResponse:
    """Maps a deal's latest analysis_run onto DealStatusResponse, keyed by
    (job_name, status) per docs/plans/analysis-pipeline-stage-chaining.md
    (point 4's combined per-document job folds extraction+the binding audit
    into `job_name == "parsing"` itself, so that row's `successful` now
    points straight at `"verification"` rather than `"classify"` — pass1's
    work already happened inside it). `job_name == "verification"` is the
    separate, deal-level 3a/3b pass (`start_deal_verification`), which only
    ever runs after a `parsing` row succeeds. `job_name == "screening"`
    (SIM-401/402/403/404) is the real last stage in the chain, and its
    `successful` row supersedes `verification`'s as the latest one once it
    exists. There is still no dedicated "complete" job or pipeline stage
    beyond that — the memo tail (governance/OFAC/drafting/scoring) has no
    job behind it yet — so a successful `screening` run is this app's
    current definition of a finished deal, and a successful `verification`
    run is treated as terminal only for a deal that hasn't reached
    screening yet. Shared by GET /{deal_id}/status and GET /pipeline —
    the pipeline table must show the same real status this endpoint does,
    not a hardcoded placeholder."""
    run = await AnalysisRunRepo(db).latest_for_deal(deal_id)
    if run is None:
        return _no_job_status()

    # started_at is the whole CHAIN's start (the parsing run's own
    # started_at), not just this latest row's own started_at -- once the
    # latest row is a verification run, its own started_at is well after
    # the chain actually began. step_durations only ever gets an entry for
    # a step whose own run has a real ended_at -- i.e. one that's actually
    # finished, never a guess at one still in progress.
    step_durations: dict[str, int] = {}
    # Bound here, not only in the else-branch below: the screening branch
    # reads it, and while that branch is only reachable when job_name is not
    # "parsing", relying on that coupling would break the moment either
    # condition moved.
    verification_run: AnalysisRun | None = None
    if run.job_name == "parsing":
        chain_started_at = run.started_at
        parsing_seconds = _run_seconds(run)
        if parsing_seconds is not None:
            step_durations["parsing"] = parsing_seconds
    else:
        parsing_run = await AnalysisRunRepo(db).latest_by_job_name(deal_id, "parsing")
        chain_started_at = parsing_run.started_at if parsing_run is not None else run.started_at
        parsing_seconds = _run_seconds(parsing_run) if parsing_run is not None else None
        if parsing_seconds is not None:
            step_durations["parsing"] = parsing_seconds
        # The verification duration must come from the VERIFICATION row, not
        # from `run` -- once screening became the latest row (SIM-404),
        # `_run_seconds(run)` would file screening's own elapsed time under
        # step_durations["verification"] and misreport it.
        verification_run = (
            run
            if run.job_name == "verification"
            else await AnalysisRunRepo(db).latest_by_job_name(deal_id, "verification")
        )
        verification_seconds = _run_seconds(verification_run) if verification_run else None
        if verification_seconds is not None:
            step_durations["verification"] = verification_seconds
    ended_at = run.ended_at

    if run.job_name == "parsing":
        if run.status == "queued":
            return DealStatusResponse(
                job_status="queued",
                current_phase=None,
                steps=_steps_for_status(None),
                started_at=chain_started_at,
            )
        if run.status == "in_progress":
            return DealStatusResponse(
                job_status="processing",
                current_phase="parsing",
                steps=_steps_for_status("parsing"),
                started_at=chain_started_at,
            )
        if run.status == "successful":
            return DealStatusResponse(
                job_status="processing",
                current_phase="verification",
                steps=_steps_for_status("verification"),
                started_at=chain_started_at,
                ended_at=ended_at,
                step_durations=step_durations,
                job_comments=run.job_comments,
            )
        return DealStatusResponse(
            job_status="error",
            current_phase="parsing",
            steps=_steps_for_status(None, failed_phase="parsing"),
            started_at=chain_started_at,
            ended_at=ended_at,
            step_durations=step_durations,
            error_message=run.error_message,
            job_comments=run.job_comments,
        )

    if run.job_name == "screening":
        # SIM-404. Both TRACKED steps (parsing, verification) are already
        # done by the time a screening row exists, so every listed step is
        # "done" and current_phase is past the end -- the same
        # "governance" shape verification-successful returns. No screening
        # step is added to PIPELINE_STEPS on purpose: that list is ported
        # from Simpero_AI_Gov_Web's pipelineSteps.ts and must stay in sync
        # with it.
        #
        # job_comments deliberately comes from the VERIFICATION run, not
        # this one. JobCommentResponse is a per-DOCUMENT shape
        # (dataSourceId/fileName), and screening is a deal-level judgment
        # with no document to attribute -- passing a screening row's own
        # comments here fails response validation outright. The screening
        # outcome is read from GET /deals/{deal_id}/screening instead.
        #
        # "successful" means job_status="complete" -- screening is the real
        # last stage in the chain (SIM-401/402/403/404), so a successful
        # screening row is this app's current definition of a finished deal.
        # "queued"/"in_progress" still just mean "processing".
        verification_comments = verification_run.job_comments if verification_run else None
        if run.status == "failed":
            return DealStatusResponse(
                job_status="error",
                current_phase="governance",
                steps=_steps_for_status("governance"),
                started_at=chain_started_at,
                ended_at=ended_at,
                step_durations=step_durations,
                error_message=run.error_message,
                job_comments=verification_comments,
            )
        return DealStatusResponse(
            job_status="complete" if run.status == "successful" else "processing",
            current_phase="governance",
            steps=_steps_for_status("governance"),
            started_at=chain_started_at,
            ended_at=ended_at,
            step_durations=step_durations,
            job_comments=verification_comments,
        )

    # job_name == "verification"
    if run.status in ("queued", "in_progress"):
        return DealStatusResponse(
            job_status="queued" if run.status == "queued" else "processing",
            current_phase="verification",
            steps=_steps_for_status("verification"),
            started_at=chain_started_at,
            step_durations=step_durations,
        )
    if run.status == "successful":
        return DealStatusResponse(
            job_status="complete",
            current_phase="governance",
            steps=_steps_for_status("governance"),
            started_at=chain_started_at,
            ended_at=ended_at,
            step_durations=step_durations,
            job_comments=run.job_comments,
        )
    return DealStatusResponse(
        job_status="error",
        current_phase="verification",
        steps=_steps_for_status(None, failed_phase="verification"),
        started_at=chain_started_at,
        ended_at=ended_at,
        step_durations=step_durations,
        error_message=run.error_message,
        job_comments=run.job_comments,
    )


def _entity_resolution_response(row: EntityResolution) -> EntityResolutionResponse:
    return EntityResolutionResponse(
        id=str(row.id),
        deal_id=str(row.deal_id),
        source=row.source,
        # Validated against the Literal, so a status the DB CHECK allows but
        # this schema does not fails loudly here rather than reaching a client.
        status=row.status,  # pyright: ignore[reportArgumentType]
        query_name=row.query_name,
        registry_id=row.registry_id,
        legal_name=row.legal_name,
        # Stored shape == wire shape, so the persisted rows are validated
        # rather than rebuilt field by field -- same call as rule_results.
        former_names=[FormerNameResponse.model_validate(f) for f in (row.former_names or [])],
        matched_on=row.matched_on,  # pyright: ignore[reportArgumentType]
        reason=row.reason,
        evidence=row.evidence,
        created_at=row.created_at,
    )


@router.post(
    "/{deal_id}/entity-resolution",
    response_model=EntityResolutionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def resolve_deal_entity(
    deal_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(get_claims),
) -> EntityResolutionResponse:
    """SIM-262: resolve this deal's company to a registry anchor (SEC EDGAR
    CIK) and record the attempt.

    The front gate of corroboration -- SIM-408's harvest, SIM-253's reconcile
    and SIM-254's roll-up all inherit this answer, so the resolver is
    deliberately conservative: an ambiguous name resolves to `unresolved` and
    checks nothing rather than guessing, because a wrong anchor poisons every
    downstream check.

    `not_found` is a 201 like any other outcome, not a 404. It is a real,
    expected answer -- most private targets have no SEC filer -- and the row
    recording that we looked is exactly as valuable as one recording a hit.
    Absence is not contradiction.

    Append-only: re-resolving a renamed or newly-filed company INSERTs a new
    row, and the older rows stay as the record of how the answer changed.

    Known tradeoff: the registry call happens inside this request's open
    transaction, so a slow SEC pins a PgBouncer slot for its duration. Capped
    by the resolver's 10s timeout (the 2026-08-16 spike ran ~2s) and fine at
    Alpha volume. If it stops being fine, this moves to a job stage -- which
    is SIM-253's call about pipeline placement anyway, not a decision to
    pre-empt here.
    """
    org_id, actor_id, actor_email, _ = await _actor(db, claims)

    deal = await DealRepo(db).get_by_id(deal_id)
    if deal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deal not found")

    try:
        resolution = await get_resolver().resolve(deal.name)
    except EntityResolutionError as exc:
        # 502, not 500: the failure is upstream at SEC, and it is explicitly
        # NOT a resolution outcome -- nothing is persisted, so a retry after
        # the registry recovers is clean rather than appending an "error" row
        # that later reads like a finding about the company.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    row = await EntityResolutionRepo(db).record(resolution, org_id=org_id, deal_id=deal_id)
    await db.flush()

    await HumanAuditRepo(db).append(
        {
            "org_id": org_id,
            "actor_id": actor_id,
            "actor_email": actor_email,
            "event_type": "deal_entity_resolved",
            "deal_id": deal_id,
            # The full resolution, so the append-only trail carries the
            # evidence independently of the entity_resolution table.
            "payload": resolution.to_json(),
        }
    )

    return _entity_resolution_response(row)


@router.get("/{deal_id}/entity-resolution", response_model=EntityResolutionResponse)
async def get_deal_entity_resolution(
    deal_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> EntityResolutionResponse:
    """SIM-262: the deal's most recent entity-resolution attempt.

    404 distinguishes the two real cases in its detail: no such deal, versus a
    deal nobody has tried to resolve yet. A deal that WAS resolved and came
    back `not_found` is a 200 carrying that status -- "we looked and found
    nothing" is an answer, and collapsing it into a 404 would make it
    indistinguishable from "we never looked".
    """
    deal = await DealRepo(db).get_by_id(deal_id)
    if deal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deal not found")

    row = await EntityResolutionRepo(db).latest_for_deal(deal_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This deal's entity has not been resolved yet",
        )

    return _entity_resolution_response(row)


@router.get("/{deal_id}/documents", response_model=list[DealDocumentResponse])
async def list_deal_documents(
    deal_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> list[DealDocumentResponse]:
    """deals.documents -> DealDocumentResponse[]. The TODO in
    useUploadDocument.ts and the "no listing endpoint exists yet" callouts
    in MaterialsCard/DataRoomPane/OverviewPane (Simpero_AI_Gov_Web) are what
    this closes. Org-side and external-intake uploads (P3-10) both land in
    data_source through the same DataSourceRepo, so this list is identical
    regardless of which path a document came in through -- nothing here
    filters or tags by origin."""
    deal = await DealRepo(db).get_by_id(deal_id)
    if deal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deal not found")

    documents = await DataSourceRepo(db).list_for_deal(deal_id)
    return [
        DealDocumentResponse(
            id=str(document.id),
            filename=document.filename,
            status=cast(DealDocumentStatus, document.status),
            created_at=document.created_at,
        )
        for document in documents
    ]


@router.get("/{deal_id}/status", response_model=DealStatusResponse)
async def get_deal_status(
    deal_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> DealStatusResponse:
    """deals.status -> DealStatusPayload. See _compute_deal_status for the
    mapping."""
    deal = await DealRepo(db).get_by_id(deal_id)
    if deal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deal not found")

    return await _compute_deal_status(db, deal_id)


@router.post(
    "/{deal_id}/analysis",
    response_model=DealStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_analysis(
    deal_id: uuid.UUID,
    body: StartAnalysisRequest,
    claims: dict[str, Any] = Depends(get_claims),
    db: AsyncSession = Depends(get_db),
) -> DealStatusResponse:
    """deals.startAnalysis. Enqueues on this app's own "simpero" queue only —
    start_deal_analysis (the worker task) does the actual fan-out to the
    parser service's "parse" queue; never enqueued here directly, same
    precedent as app/api/uploads.py's complete_upload."""
    deal = await DealRepo(db).get_by_id(deal_id)
    if deal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deal not found")

    analysis_repo = AnalysisRunRepo(db)
    if await analysis_repo.active_for_deal(deal_id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Analysis is already running for this deal",
        )

    pending_link = await IntakeLinkRepo(db).get_pending_for_deal_unlocked(deal_id)
    if pending_link is not None and compute_intake_link_effective_status(pending_link) == "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot start analysis while an intake link is still pending for this deal",
        )

    data_sources = await DataSourceRepo(db).list_for_deal(deal_id)
    usable = [ds for ds in data_sources if ds.status == "verified"]
    pending = [ds for ds in data_sources if ds.status == "pending"]

    if not usable and not pending:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Upload at least one document before starting analysis",
        )
    if not usable:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Documents are still being verified — try again in a moment",
        )

    org_id, actor_id, actor_email, _ = await _actor(db, claims)

    try:
        run = await analysis_repo.create(
            {
                "id": uuid.uuid4(),
                "org_id": org_id,
                "deal_id": deal_id,
                "job_name": "parsing",
                "selected_frameworks": body.selected_frameworks,
                "status": "queued",
            }
        )
        # Forces uq_analysis_run_active's constraint check now, inside this
        # try block, rather than at the transaction's final commit (outside
        # any handler of ours) — D6's actual double-submit guarantee, for the
        # race the fast-path SELECT above can't catch.
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Analysis is already running for this deal",
        ) from exc

    # "simpero" queue -- this app's own SAQ worker, never the "parse" queue
    # (app/jobs/parse_client.py). Explicit timeout/retries/ttl: SAQ's default
    # timeout is 10 seconds, far short of this task's multi-hour fan-out+poll.
    await get_queue().enqueue(
        "start_deal_analysis",
        analysis_run_id=str(run.id),
        deal_id=str(deal_id),
        clerk_org_id=claims["tenant_id"],
        timeout=7200,
        retries=1,
        ttl=86400,
    )

    await HumanAuditRepo(db).append(
        {
            "org_id": org_id,
            "actor_id": actor_id,
            "actor_email": actor_email,
            "event_type": "analysis_requested",
            "deal_id": deal_id,
            "payload": {"analysis_run_id": str(run.id), "document_count": len(usable)},
        }
    )

    return DealStatusResponse(
        job_status="queued",
        current_phase=None,
        steps=_steps_for_status(None),
        started_at=run.started_at,
    )


@router.post(
    "/{deal_id}/intake-link",
    response_model=CreateIntakeLinkResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_intake_link(
    deal_id: uuid.UUID,
    body: CreateIntakeLinkRequest,
    claims: dict[str, Any] = Depends(get_claims),
    db: AsyncSession = Depends(get_db),
) -> CreateIntakeLinkResponse:
    """P3-01. Generates the external deal-intake link -- one live (`pending`)
    link per deal, enforced by ux_deal_intake_link_pending_deal (partial
    unique index) at insert time, not a fast-path check here for the still-
    live case (let the constraint reject it, same idiom as start_analysis's
    uq_analysis_run_active)."""
    deal = await DealRepo(db).get_by_id(deal_id)
    if deal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deal not found")

    # Deliberately latest_for_deal, not active_for_deal -- ANY analysis_run
    # row (any status) blocks link generation, not just a currently-running
    # one. Flagged separately as a product question needing later
    # confirmation; implemented as specified here.
    if await AnalysisRunRepo(db).latest_for_deal(deal_id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot generate an intake link once analysis has started for this deal",
        )

    intake_link_repo = IntakeLinkRepo(db)
    existing = await intake_link_repo.get_pending_for_deal(deal_id)
    reissued = False
    if existing is not None and existing.expires_at <= datetime.now(UTC):
        # Lazy-expire: flip the stale row to a terminal status and flush now,
        # so its UPDATE commits within this transaction before the new
        # link's INSERT is attempted -- otherwise both rows would be
        # `pending` at once and the partial unique index would reject the
        # insert. A still-live pending link is left alone here; the insert
        # below is what rejects that case (via the same index).
        await intake_link_repo.mark_expired(existing)
        await db.flush()
        reissued = True

    org_id, actor_id, actor_email, user_id = await _actor(db, claims)

    questions = await DealIntakeQuestionRepo(db).list_active()
    questions_snapshot = {
        "snapshot_version": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "questions": [
            {
                "question_key": q.question_key,
                "prompt": q.prompt,
                "help_text": q.help_text,
                "input_type": q.input_type,
                "required": q.required,
                "display_order": q.display_order,
            }
            for q in questions
        ],
    }

    raw_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(days=_INTAKE_LINK_TTL_DAYS)

    try:
        link = await intake_link_repo.create(
            {
                "id": uuid.uuid4(),
                "org_id": org_id,
                "clerk_org_id": claims["tenant_id"],
                "deal_id": deal_id,
                "token_hash": sha256_hex(raw_token),
                "recipient_email": body.recipient_email,
                "questions_snapshot": questions_snapshot,
                "expires_at": expires_at,
                "created_by_user_id": user_id,
            }
        )
        # Forces ux_deal_intake_link_pending_deal's constraint check now,
        # inside this try block, rather than at the transaction's final
        # commit outside any handler of ours -- same idiom as
        # start_analysis's uq_analysis_run_active flush.
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An active intake link already exists for this deal",
        ) from exc

    await HumanAuditRepo(db).append(
        {
            "org_id": org_id,
            "actor_id": actor_id,
            # Deliberate override: created_by_user_id on the link row already
            # captures the actor; this audit row's actor_email stays unset.
            "actor_email": None,
            "event_type": "intake_link_reissued" if reissued else "intake_link_generated",
            "deal_id": deal_id,
            "payload": {"intake_link_id": str(link.id), "recipient_email": body.recipient_email},
        }
    )

    return CreateIntakeLinkResponse(
        id=str(link.id),
        token=raw_token,
        status=compute_intake_link_effective_status(link),
        expires_at=link.expires_at,
    )
