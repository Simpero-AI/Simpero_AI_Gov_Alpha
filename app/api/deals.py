import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_claims, get_db
from app.jobs.queue import get_queue
from app.models.analysis_run import AnalysisRun
from app.models.deal import Deal
from app.repo.AnalysisRunRepo import AnalysisRunRepo
from app.repo.DataSourceRepo import DataSourceRepo
from app.repo.DealRepo import DealRepo
from app.repo.HumanAuditRepo import HumanAuditRepo
from app.repo.ScreeningResultRepo import ScreeningResultRepo
from app.repo.SessionRepo import SessionRepo
from app.repo.UserRepo import UserRepo
from app.schemas.deals import (
    AvgAiScoreStat,
    CreateDealRequest,
    CreateDealResponse,
    DashboardStatsResponse,
    DdCompletionStat,
    DealRowResponse,
    DealStatusResponse,
    DealWithLatestMemoResponse,
    LatestMemoSessionResponse,
    LivePipelineRowResponse,
    PipelineStepResponse,
    PipelineValueStat,
    ScreeningResultResponse,
    StartAnalysisRequest,
    UpdateDealRequest,
    ValueDelta,
)
from app.services.dashboard_stats import compute_month_bounds, compute_pipeline_value_delta
from app.services.memo_summary import derive_pipeline_metrics
from app.services.pipeline_steps import no_job_steps
from app.services.screening.rule_view import enrich_rule_results
from app.services.screening.rulebook import load_rulebook

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


def _deal_row_response(deal: Deal, lead_name: str | None) -> DealRowResponse:
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
        lead_user_id=deal.lead_user_id,
        lead_name=lead_name,
        referred_by=deal.referred_by,
        created_at=deal.created_at,
        updated_at=deal.updated_at,
    )


async def _validate_lead_user_id(db: AsyncSession, lead_user_id: int) -> None:
    """lead_user_id must resolve under the caller's own RLS scope -- i.e.
    actually be a user in the caller's org, not just any users.id (a raw FK
    constraint wouldn't catch this: FK checks aren't RLS-scoped)."""
    if await UserRepo(db).get_by_id(lead_user_id) is None:
        raise HTTPException(status_code=422, detail="Invalid leadUserId")


async def _lead_name(db: AsyncSession, lead_user_id: int | None) -> str | None:
    """Read-side join for DealRowResponse.lead_name -- deliberately doesn't
    raise if the user is gone (e.g. later removed): a stale lead reference
    should degrade to a null name, not break GET/PATCH on the deal."""
    if lead_user_id is None:
        return None
    user = await UserRepo(db).get_by_id(lead_user_id)
    return user.name if user else None


@router.post("", response_model=CreateDealResponse, status_code=status.HTTP_201_CREATED)
async def create_deal(
    body: CreateDealRequest,
    claims: dict[str, Any] = Depends(get_claims),
    db: AsyncSession = Depends(get_db),
) -> CreateDealResponse:
    """deals.create. fund_id stays null — the frontend's contract has no fund
    field yet (see the comment on Deal.fund_id)."""
    org_id, actor_id, actor_email, user_id = await _actor(db, claims)

    if body.lead_user_id is not None:
        await _validate_lead_user_id(db, body.lead_user_id)

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
            "lead_user_id": body.lead_user_id,
            "referred_by": body.referred_by,
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
    lead_name = await _lead_name(db, deal.lead_user_id)
    return DealWithLatestMemoResponse(
        deal=_deal_row_response(deal, lead_name), latest_memo_session=latest_memo_session
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
    if updates.get("lead_user_id") is not None:
        await _validate_lead_user_id(db, updates["lead_user_id"])

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

    lead_name = await _lead_name(db, updated.lead_user_id)
    return _deal_row_response(updated, lead_name)


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
