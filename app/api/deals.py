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
    StartAnalysisRequest,
    ValueDelta,
)
from app.services.dashboard_stats import compute_month_bounds, compute_pipeline_value_delta
from app.services.memo_summary import derive_pipeline_metrics
from app.services.pipeline_steps import no_job_steps

router = APIRouter(prefix="/deals", tags=["deals"])


async def _actor(db: AsyncSession, claims: dict[str, Any]) -> tuple[int, str, str | None]:
    """(org_id, actor_id, actor_email) for the audit rows this router appends."""
    user = await UserRepo(db).get_by_clerk_id(claims["user_id"])
    assert user is not None  # get_db JIT-provisions this row before the handler runs
    return user.org_id, claims["user_id"], user.email


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
    org_id, actor_id, actor_email = await _actor(db, claims)

    deal = await DealRepo(db).create(
        {
            "id": uuid.uuid4(),
            "org_id": org_id,
            "name": body.name,
            "gp_source": body.gp_source,
            "deal_size_min_usd": body.deal_size_min_usd,
            "deal_size_max_usd": body.deal_size_max_usd,
            "sector_tags": body.sector_tags,
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
        # ponytail: one query per deal for its latest session (N+1) — fine at
        # pipeline-table scale; batch this (single query with a lateral join
        # or window function) if the pipeline table gets large.
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
                agent_status=_no_job_status(),
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

    org_id, actor_id, actor_email = await _actor(db, claims)
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


@router.get("/{deal_id}/status", response_model=DealStatusResponse)
async def get_deal_status(
    deal_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> DealStatusResponse:
    """deals.status -> DealStatusPayload. Maps the deal's latest
    analysis_run onto this shape, keyed by (job_name, status) per
    docs/plans/analysis-pipeline-stage-chaining.md (point 4's combined
    per-document job folds extraction+the binding audit into `job_name
    == "parsing"` itself, so that row's `successful` now points straight
    at `"verification"` rather than `"classify"` — pass1's work already happened
    inside it). `job_name == "verification"` is the separate, deal-level
    3a/3b pass (`start_deal_verification`), which only ever runs after a
    `parsing` row succeeds. Neither ever maps to `"complete"` — the memo
    tail (governance/OFAC/drafting/scoring) has no job behind it yet."""
    deal = await DealRepo(db).get_by_id(deal_id)
    if deal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deal not found")

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
        verification_seconds = _run_seconds(run)
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
            job_status="processing",
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

    org_id, actor_id, actor_email = await _actor(db, claims)

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
