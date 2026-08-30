"""Corroboration (SIM-416): the external-corroboration stage, chained between
verification and screening.

Why a dedicated job rather than a step inside the verify transaction: the
corroboration sources reach out over the network (SEC EDGAR, ISED, USPTO,
Federal Register, ...), and holding a Postgres transaction open across a
multi-second HTTP round-trip is the classic way to exhaust the connection pool
and trip idle-in-transaction timeouts. So this job runs in three phases on ONE
session:

  Phase A (prime, read-only transaction) -- load the run + the deal's
    corroboratable claims + prime the resolved-entity cache, then COMMIT so no
    transaction is held open across the network below. Committing a read-only
    transaction is the point: it releases the connection's snapshot before the
    HTTP round-trips start.

  Phase B (gather, NO transaction) -- run every registered source over every
    corroboratable claim and collect verdicts, writing nothing. This is the
    network I/O, held deliberately outside any DB transaction. Sources read only
    the claim columns already loaded (expire_on_commit=False keeps them populated
    after Phase A commits) plus their primed, in-memory per-deal context, so no
    query fires mid-gather.

  Phase C (write, short transaction) -- re-open RLS scope, re-SELECT the claims,
    record the gathered verdicts (marking any disagreed claim `conflicted`), and
    re-run the deal roll-up so trust statuses reflect the new events. DB-only and
    quick; the network is already done.

Phase B runs the registered CORROBORATION_SOURCES (SEC EDGAR today) over the
deal's corroboratable claims, so it makes HTTP calls only when a deal actually
has claims a source can speak to; a claim-less deal, or claims no source matches,
still gathers nothing and this job is a behaviour-preserving pass-through that
re-rolls-up and chains into screening. Because the network is in Phase B (no txn
open), a slow or flaky source can never hold the write transaction open.

Same durability posture as the verify/screening jobs: any failure -- an
exception or the SAQ timeout cancelling this coroutine -- durably records a
terminal `failed` status, so the frontend never hangs on "loading results".
"""

import asyncio
import logging
from collections import Counter
from uuid import UUID, uuid4

from saq.types import Context
from sqlalchemy import select, text

from app.core.database import AsyncSessionLocal
from app.jobs.queue import get_queue
from app.models import Claim
from app.repo.AnalysisRunRepo import AnalysisRunRepo
from app.repo.HumanAuditRepo import HumanAuditRepo
from app.services.corroboration import (
    CORROBORATABLE_STATUSES,
    CORROBORATION_SOURCES,
    apply_corroboration,
    gather_corroboration,
)
from app.services.entity_resolution.resolved import load_resolved_entity
from app.services.status_rollup import roll_up_deal

logger = logging.getLogger(__name__)

# Per-statement ceiling for this job's DB transactions (Phase A read, Phase C
# write), set well under the SAQ job timeout so a blocked or runaway query aborts
# as a normal error (caught and recorded as `failed` by the wrapper) instead of
# hanging the job -- and the UI -- indefinitely. Mirrors
# start_deal_verification._STATEMENT_TIMEOUT / start_deal_screening's. Does NOT
# bound Phase B: the network gather runs with no transaction open, so no
# statement_timeout applies there -- the SAQ job timeout is its only ceiling.
_STATEMENT_TIMEOUT = "120s"


async def _set_org(session, clerk_org_id: str) -> None:
    await session.execute(
        text("SELECT set_config('app.org_id', :tid, true)"),
        {"tid": clerk_org_id},
    )


async def start_deal_corroboration(
    ctx: Context,
    *,
    analysis_run_id: str,
    clerk_org_id: str,
) -> None:
    """SAQ entrypoint. Thin wrapper whose only job is to guarantee that ANY
    failure of the corroboration work -- an exception in Phase C, or the SAQ
    timeout cancelling this coroutine -- durably records a terminal `failed`
    status. The write work runs in one transaction (Phase C); when that rolls
    back it takes its own progress markers with it, so without this wrapper the
    run reverts to `queued` and stays non-terminal, and GET /deals/{id}/status
    reports `processing` forever, leaving the frontend stuck on "loading
    results". Exactly the hang the verify/screening wrappers already prevent.
    CancelledError is caught alongside Exception because SAQ enforces its job
    timeout by cancelling this coroutine, and CancelledError is a
    BaseException."""
    run_id = UUID(analysis_run_id)
    try:
        await _run_corroboration(analysis_run_id=analysis_run_id, clerk_org_id=clerk_org_id)
    except (Exception, asyncio.CancelledError) as exc:
        await _mark_run_failed(run_id, clerk_org_id, exc)
        raise


async def _run_corroboration(*, analysis_run_id: str, clerk_org_id: str) -> None:
    """`deal_id` is deliberately not a parameter -- org_id/deal_id both come from
    the run row itself, same reasoning as start_deal_verification/screening."""
    run_id = UUID(analysis_run_id)

    async with AsyncSessionLocal() as session:
        # --- Phase A: prime (read-only transaction) ---------------------------
        async with session.begin():
            await session.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"))
            await _set_org(session, clerk_org_id)
            run_repo = AnalysisRunRepo(session)

            run = await run_repo.get_by_id(run_id)
            if run is None:
                raise ValueError(f"analysis_run {analysis_run_id} not found")

            # Idempotency guard: a SAQ redelivery after this run already reached a
            # terminal status must not re-issue the network gather or re-append
            # corroboration events (CorroborationEvent is append-only, so a repeat
            # would silently double-record a verdict). Returning here also makes
            # the wrapper's `failed` write terminal -- a retry after a recorded
            # failure is a clean no-op.
            if run.status in ("successful", "failed"):
                return

            org_id, deal_uuid = run.org_id, run.deal_id

            # The corroboratable claims, read into memory so the gather below can
            # iterate them with no transaction open. expire_on_commit=False (see
            # app/core/database.py) keeps their column values populated after this
            # transaction commits, so the sources can read claim.value/.entity in
            # Phase B without a lazy refresh hitting the closed transaction.
            claim_stmt = (
                select(Claim)
                .where(Claim.deal_id == deal_uuid)
                .where(Claim.status.in_(sorted(CORROBORATABLE_STATUSES)))
            )
            claims = list((await session.scalars(claim_stmt)).all())

            # Prime the per-deal context every current source reads -- this deal's
            # resolved identity -- INSIDE this transaction, memoized on
            # session.info (which outlives the transaction). This is what lets
            # Phase B honour the no-DB-during-gather invariant: load_resolved_entity
            # then returns the cached value without a query, so no source
            # autobegins a transaction that the network round-trips would hold
            # open. A future source needing different per-deal context primes it
            # here too.
            await load_resolved_entity(session, deal_uuid)

        # --- Phase B: gather (NO transaction open) ----------------------------
        # The network I/O, held deliberately outside any DB transaction. `session`
        # is passed only so sources can read their primed, in-memory context; a
        # source that issues a real query here breaks the invariant (and would
        # autobegin a transaction the HTTP round-trips then hold open). Returns []
        # (no HTTP) for a claim-less deal or claims no registered source matches.
        gathered = await gather_corroboration(session, claims, CORROBORATION_SOURCES)

        # --- Phase C: write (short transaction) -------------------------------
        async with session.begin():
            await session.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"))
            # SET LOCAL is transaction-scoped, so the org scope set in Phase A did
            # not survive its commit -- re-establish RLS for this transaction.
            await _set_org(session, clerk_org_id)
            run_repo = AnalysisRunRepo(session)

            run = await run_repo.get_by_id(run_id)
            if run is None or run.status in ("successful", "failed"):
                return

            # Re-SELECT the claims so the verdicts land on objects attached to
            # THIS transaction -- the Phase A objects belong to a committed,
            # closed transaction, and record_corroboration_result mutates
            # claim.status, which must happen on a session-attached claim.
            rollup_stmt = (
                select(Claim)
                .where(Claim.deal_id == deal_uuid)
                .where(Claim.status.in_(sorted(CORROBORATABLE_STATUSES)))
            )
            rollup_claims = list((await session.scalars(rollup_stmt)).all())

            await apply_corroboration(session, rollup_claims, gathered)
            # Load-bearing, not defensive: AsyncSessionLocal sets autoflush=False,
            # so the events apply_corroboration just wrote are invisible to
            # roll_up_deal's own SELECT until they hit the DB.
            await session.flush()
            await roll_up_deal(session, rollup_claims)
            rollup_counts: Counter[str] = Counter(c.status for c in rollup_claims)
            await session.flush()

            await run_repo.update_progress(run_id, status="successful")
            await HumanAuditRepo(session).append(
                {
                    "org_id": org_id,
                    "actor_id": "Internal System",
                    "actor_email": "Internal System",
                    "event_type": "analysis_corroboration_completed",
                    "deal_id": deal_uuid,
                    "payload": {
                        "analysis_run_id": analysis_run_id,
                        "status": "successful",
                        "sources": [getattr(s, "name", str(s)) for s in CORROBORATION_SOURCES],
                        "verdicts_recorded": len(gathered),
                        "status_rollup": dict(rollup_counts),
                    },
                }
            )

            # Chain into screening (SIM-404). The row is created HERE, inside the
            # transaction that just marked this run terminal: uq_analysis_run_active
            # is a partial unique index on deal_id ALONE (WHERE status IN
            # queued/in_progress), so the screening row can only be legal once this
            # run is no longer active. The explicit flush below forces this run's
            # `successful` UPDATE to the DB BEFORE the screening INSERT, so the two
            # rows are never simultaneously active against that index. Same
            # hand-off pattern verify used to reach this job.
            await session.flush()
            screening_run_id = uuid4()
            await run_repo.create(
                {
                    "id": screening_run_id,
                    "org_id": org_id,
                    "deal_id": deal_uuid,
                    "job_name": "screening",
                    "status": "queued",
                }
            )
            await HumanAuditRepo(session).append(
                {
                    "org_id": org_id,
                    "actor_id": "Internal System",
                    "actor_email": "Internal System",
                    "event_type": "analysis_requested",
                    "deal_id": deal_uuid,
                    "payload": {"analysis_run_id": str(screening_run_id), "job_name": "screening"},
                }
            )

    # Outside the transaction -- screening_run_id's row is now durably committed,
    # so a worker that dequeues this immediately is guaranteed to find it.
    # Enqueuing inside the `async with` above let a worker outrace the commit and
    # fail with "analysis_run ... not found".
    await get_queue().enqueue(
        "start_deal_screening",
        analysis_run_id=str(screening_run_id),
        clerk_org_id=clerk_org_id,
        timeout=3600,
        retries=1,
        ttl=86400,
    )


async def _mark_run_failed(run_id: UUID, clerk_org_id: str, exc: BaseException) -> None:
    """Record a terminal `failed` status for the run in its OWN fresh transaction
    -- Phase C has already rolled back, so its progress markers are gone and the
    run would otherwise sit non-terminal (the frontend hangs on "loading results"
    indefinitely). Idempotent (skips a run that already reached a terminal status,
    so a SAQ retry after this is a clean no-op via _run_corroboration's own guard)
    and best-effort: if even this write fails it is logged, never raised, so it
    can never mask the real failure the caller re-raises. error_message carries
    only the exception TYPE -- never str(exc) -- to keep document-derived content
    out of a persisted field. Mirrors start_deal_verification._mark_run_failed."""
    try:
        async with AsyncSessionLocal() as session, session.begin():
            await session.execute(text("SET LOCAL statement_timeout = '30s'"))
            await _set_org(session, clerk_org_id)
            run_repo = AnalysisRunRepo(session)
            run = await run_repo.get_by_id(run_id)
            if run is None or run.status in ("successful", "failed"):
                return
            await run_repo.update_progress(
                run_id,
                status="failed",
                error_message=f"corroboration failed: {type(exc).__name__}",
            )
            await HumanAuditRepo(session).append(
                {
                    "org_id": run.org_id,
                    "actor_id": "Internal System",
                    "actor_email": "Internal System",
                    "event_type": "analysis_corroboration_completed",
                    "deal_id": run.deal_id,
                    "payload": {"analysis_run_id": str(run_id), "status": "failed"},
                }
            )
    except Exception:
        logger.exception("could not record corroboration failure for run %s", run_id)
