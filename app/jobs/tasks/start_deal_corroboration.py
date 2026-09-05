"""Corroboration (Epic 12): the external-source pass, chained BETWEEN verification
and screening (verify -> corroboration -> screening).

Runs the registered public-source adapters (SEC EDGAR, ISED Corporations Canada,
US Federal Register, CIPO/USPTO trademarks) over the deal's internally-checked
claims, records each agree/disagree as an append-only corroboration_event, and
re-runs the deal roll-up so an externally-conflicted claim reaches screening as
`conflicted` rather than trusted. It sits BEFORE screening on purpose: screening
reads rolled-up claim trust, so the corroboration verdicts must land first.

It ALSO runs the web-search deep-search COLLECT pass (web_search_collect): a
bounded, allowlisted web search that mints NEW cited `web` claims for the Market
and Company tabs (sizing, competitors, market definition, company overview/
risks/related-parties/plans) even when the deck itself is thin. Collection is
best-effort enrichment and a no-op without an anthropic key, so it never gates
the pipeline.

I/O placement (SIM-253): the external HTTP must NOT run inside a DB transaction --
holding a pooled PgBouncer backend across a slow network call is the anti-pattern
start_deal_analysis documents. So this job runs THREE phases on ONE session
(AsyncSessionLocal is expire_on_commit=False, so the loaded claims stay usable
across the phase boundaries):
  A. a short READ transaction: load the corroboratable claims, prime the
     session-memoized entity cache (load_resolved_entity) once.
  B. NO transaction: gather every source's verdict over those claims -- ALL HTTP
     happens here; the adapters' only DB touch (load_resolved_entity) is a cache hit.
  C. a short WRITE transaction: append the events, re-run the roll-up.

Run-row-less by design: corroboration has no analysis_run row of its own (that
would need a ck_analysis_run_job_name migration and would hijack the FE status
ladder via latest_for_deal). It keys off the SCREENING run row verify already
created (queued), and hands off to screening on completion. Corroboration is
best-effort enrichment: ANY failure still enqueues screening, so a flaky external
source can never stall the pipeline.
"""

import asyncio
import logging
from uuid import UUID

from saq.types import Context
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.jobs.queue import get_queue
from app.models.claim import Claim
from app.repo.AnalysisRunRepo import AnalysisRunRepo
from app.repo.DealRepo import DealRepo
from app.repo.HumanAuditRepo import HumanAuditRepo
from app.services.corroboration import (
    CORROBORATABLE_STATUSES,
    gather_corroboration,
    persist_corroboration,
)
from app.services.corroboration_sources import DEFAULT_SOURCES
from app.services.entity_resolution.resolved import load_resolved_entity
from app.services.status_rollup import roll_up_deal
from app.services.web_search_collect import gather_web_facts, persist_web_facts

logger = logging.getLogger(__name__)

# Per-statement ceiling for the two short DB transactions (A and C), well under the
# SAQ job timeout. The external HTTP runs in phase B with NO transaction, so this
# never guillotines a network wait. Mirrors the sibling jobs' _STATEMENT_TIMEOUT.
_STATEMENT_TIMEOUT = "120s"

# How screening is (re-)enqueued after corroboration -- identical to how verify
# enqueues screening today.
_SCREENING_TIMEOUT = 3600
_SCREENING_TTL = 86400


async def _set_org(session, clerk_org_id: str) -> None:
    await session.execute(
        text("SELECT set_config('app.org_id', :tid, true)"),
        {"tid": clerk_org_id},
    )


async def start_deal_corroboration(
    ctx: Context,
    *,
    screening_run_id: str,
    clerk_org_id: str,
) -> None:
    """SAQ entrypoint. Runs the corroboration pass, then hands off to screening.

    Unlike verify/screening this job has NO analysis_run row of its own, so there is
    no _mark_run_failed and nothing to leave non-terminal: it keys off the SCREENING
    run row verify created. The wrapper logs any failure and STILL enqueues screening
    (best-effort enrichment must never stall the pipeline). CancelledError is caught
    alongside Exception because SAQ enforces its timeout by cancelling the coroutine,
    and CancelledError is a BaseException."""
    run_id = UUID(screening_run_id)
    try:
        hand_off = await _run_corroboration(screening_run_id=run_id, clerk_org_id=clerk_org_id)
    except (Exception, asyncio.CancelledError):
        logger.exception(
            "corroboration failed for screening run %s; proceeding to screening", run_id
        )
        hand_off = True
    if hand_off:
        await get_queue().enqueue(
            "start_deal_screening",
            analysis_run_id=str(run_id),
            clerk_org_id=clerk_org_id,
            timeout=_SCREENING_TIMEOUT,
            retries=1,
            ttl=_SCREENING_TTL,
        )


async def _run_corroboration(*, screening_run_id: UUID, clerk_org_id: str) -> bool:
    """Returns True if the caller should enqueue screening. Returns False ONLY when
    the screening run is already past `queued` -- i.e. a SAQ redelivery after this
    job already handed off -- so we neither re-corroborate (which would append
    duplicate append-only events) nor re-enqueue screening."""
    settings = get_settings()
    async with AsyncSessionLocal() as session:
        # --- Phase A: short READ transaction ---
        async with session.begin():
            await session.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"))
            await _set_org(session, clerk_org_id)
            run = await AnalysisRunRepo(session).get_by_id(screening_run_id)
            if run is None or run.status != "queued":
                # Screening already handed off (redelivery) or the row is gone --
                # do not re-run or re-enqueue.
                return False
            deal_uuid, org_id = run.deal_id, run.org_id
            deal = await DealRepo(session).get_by_id(deal_uuid)
            company = deal.name if deal is not None else ""
            sector = deal.sector if deal is not None else None
            claims = list(
                (
                    await session.scalars(
                        select(Claim)
                        .where(Claim.deal_id == deal_uuid)
                        .where(Claim.status.in_(sorted(CORROBORATABLE_STATUSES)))
                    )
                ).all()
            )
            # Prime the session-memoized entity resolution ONCE, so the ISED/Trademark
            # adapters' load_resolved_entity in phase B is a cache hit, not a query.
            if claims:
                await load_resolved_entity(session, deal_uuid)

        # --- Phase B: NO transaction -- all external HTTP happens here ---
        # Corroboration verdicts over the deck's claims (skipped when there are
        # none), plus the web-search COLLECT pass, which mints NEW cited web
        # claims regardless of the deck and is a no-op without an anthropic key.
        results = await gather_corroboration(session, claims, DEFAULT_SOURCES) if claims else []
        web_candidates = await gather_web_facts(
            company=company,
            sector=sector,
            api_key=settings.anthropic_api_key,
            model=settings.web_search_model,
        )

        # --- Phase C: short WRITE transaction ---
        if results or web_candidates:
            async with session.begin():
                # SET LOCAL and RLS (SET LOCAL app.org_id) are per-transaction, so
                # re-issue them as the first statements of this new transaction.
                await session.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"))
                await _set_org(session, clerk_org_id)
                minted = 0
                if results:
                    await persist_corroboration(session, results, {c.id: c for c in claims})
                    # flush so the appended events + conflicted statuses are visible
                    # to roll_up_deal's own SELECTs before it reads them.
                    await session.flush()
                    await roll_up_deal(session, claims)
                    await session.flush()
                if web_candidates:
                    # Isolate the web mint in a SAVEPOINT so a failure here (e.g. a
                    # constraint violation from a malformed collected fact) rolls
                    # back only the web claims, never the corroboration events +
                    # roll-up already written in this transaction. Best-effort
                    # enrichment must not discard real corroboration verdicts.
                    try:
                        async with session.begin_nested():
                            minted = await persist_web_facts(
                                session, deal_id=deal_uuid, org_id=org_id, candidates=web_candidates
                            )
                            await session.flush()
                    except Exception:
                        logger.warning(
                            "web collect persist failed for screening run %s; "
                            "corroboration verdicts kept",
                            screening_run_id,
                            exc_info=True,
                        )
                        minted = 0
                await HumanAuditRepo(session).append(
                    {
                        "org_id": org_id,
                        "actor_id": "Internal System",
                        "actor_email": "Internal System",
                        "event_type": "analysis_corroboration_completed",
                        "deal_id": deal_uuid,
                        "payload": {
                            "screening_run_id": str(screening_run_id),
                            "claims_checked": len(claims),
                            "events_recorded": len(results),
                            "web_facts_collected": minted,
                        },
                    }
                )
    return True
