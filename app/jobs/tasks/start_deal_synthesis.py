"""Synthesis (W3 of the consistency effort): the field-synthesis pass, chained
AFTER screening as the new pipeline tail (verify -> corroboration -> screening ->
synthesis).

It computes the grounded Company + Summary narrative sections ONCE, here, and
freezes them in a synthesis_snapshot row. GET /deals/{id}/company-synthesis then
becomes a pure reader of the latest snapshot -- no LLM, no retrieval on a page
load -- so two loads of the same deal are byte-identical and a re-analysis
appends a new snapshot the reader supersedes to (never a request-time re-roll).

I/O placement (same discipline as corroboration): the ~45s-per-section LLM gather
must NOT run inside a DB transaction -- holding a pooled PgBouncer backend across a
slow network call trips idle_in_transaction_session_timeout. So this runs THREE
phases on ONE session (AsyncSessionLocal is expire_on_commit=False):
  A. a short READ transaction: load the deal + its document ids, retrieve the
     per-section chunks (org-scoped, so the read must be inside the scoped txn).
  B. NO transaction: the parallel grounded LLM gather -- all HTTP happens here.
  C. a short WRITE transaction: INSERT the snapshot (runs UNCONDITIONALLY, so an
     empty/failed pass still records WHY via the reason sentinel).

Run-row-less by design (like corroboration): no analysis_run row of its own -- one
would need a ck_analysis_run_job_name migration and would hijack the FE status
ladder via latest_for_deal. It keys off the SCREENING run id it is handed, purely
for snapshot provenance. It is the pipeline TAIL and best-effort: any failure is
logged and swallowed (nothing hands off after it, and the GET fails soft to the
claims-driven view when no snapshot exists), so synthesis can never stall or fail
the deal -- a successful screening is already the deal's definition of complete.
"""

import asyncio
import logging
from uuid import UUID

from saq.types import Context
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.repo.AnalysisRunRepo import AnalysisRunRepo
from app.repo.DataSourceRepo import DataSourceRepo
from app.repo.DealRepo import DealRepo
from app.repo.SynthesisSnapshotRepo import SynthesisSnapshotRepo
from app.services.field_synthesis import (
    generate,
    retrieve,
    sections_to_json,
    snapshot_reason,
)

logger = logging.getLogger(__name__)

# Per-statement ceiling for the two short DB transactions (A and C), well under the
# SAQ job timeout. The LLM gather runs in phase B with NO transaction, so this never
# guillotines a network wait. Mirrors the sibling jobs' _STATEMENT_TIMEOUT.
_STATEMENT_TIMEOUT = "120s"


async def _set_org(session, clerk_org_id: str) -> None:
    await session.execute(
        text("SELECT set_config('app.org_id', :tid, true)"),
        {"tid": clerk_org_id},
    )


async def start_deal_synthesis(
    ctx: Context,
    *,
    analysis_run_id: str,
    clerk_org_id: str,
) -> None:
    """SAQ entrypoint. Computes + persists the deal's synthesis snapshot.

    The pipeline tail: it enqueues nothing and holds no run row, so there is no
    _mark_run_failed and nothing to hand off. Any failure is logged and swallowed
    -- best-effort enrichment must never stall or fail a deal that has already
    completed screening. CancelledError is caught alongside Exception because SAQ
    enforces its timeout by cancelling the coroutine."""
    try:
        await _run_synthesis(analysis_run_id=UUID(analysis_run_id), clerk_org_id=clerk_org_id)
    except (Exception, asyncio.CancelledError):
        logger.exception(
            "synthesis snapshot failed for run %s; deal stays complete, GET falls back "
            "to the claims-driven view",
            analysis_run_id,
        )


async def _run_synthesis(*, analysis_run_id: UUID, clerk_org_id: str) -> None:
    settings = get_settings()
    async with AsyncSessionLocal() as session:
        # --- Phase A: short READ transaction (org-scoped retrieval lives here) ---
        async with session.begin():
            await session.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"))
            await _set_org(session, clerk_org_id)
            run = await AnalysisRunRepo(session).get_by_id(analysis_run_id)
            if run is None:
                logger.warning("synthesis: run %s not found; skipping", analysis_run_id)
                return
            deal_uuid, org_id = run.deal_id, run.org_id
            deal = await DealRepo(session).get_by_id(deal_uuid)
            company = deal.name if deal is not None else ""
            data_sources = await DataSourceRepo(session).list_for_deal(deal_uuid)
            document_ids = [str(ds.id) for ds in data_sources]
            # Retrieval must run inside the scoped transaction (org_scoped_search
            # asserts app.org_id). Skip it entirely when there's no key or no docs
            # -- there is nothing to ground against, and the reason records why.
            retrieved = (
                await retrieve(session, org_id=clerk_org_id, document_ids=document_ids)
                if settings.anthropic_api_key and document_ids
                else []
            )

        # --- Phase B: NO transaction -- the parallel grounded LLM gather ---
        sections = (
            await generate(
                api_key=settings.anthropic_api_key,
                model=settings.field_synthesis_model,
                company=company,
                retrieved=retrieved,
            )
            if retrieved
            else []
        )

        reason = snapshot_reason(
            has_api_key=bool(settings.anthropic_api_key),
            has_documents=bool(document_ids),
            sections=sections,
        )

        # --- Phase C: short WRITE transaction. Runs UNCONDITIONALLY -- an empty or
        # failed pass still writes a row carrying the reason sentinel, so a blank
        # page is a recorded fact ("checked, found nothing, here's why") rather than
        # indistinguishable from a not-yet-computed one. org_id is stamped explicitly
        # so the FORCE-RLS INSERT is accepted (same idiom as persist_web_facts). ---
        async with session.begin():
            await session.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"))
            await _set_org(session, clerk_org_id)
            await SynthesisSnapshotRepo(session).record(
                org_id=org_id,
                deal_id=deal_uuid,
                analysis_run_id=analysis_run_id,
                sections=sections_to_json(sections),
                reason=reason,
            )
        logger.info(
            "synthesis snapshot for %r: %d/%d sections grounded (reason=%s)",
            company,
            len(sections),
            len(retrieved),
            reason,
        )
