"""Ingest + verify job (docs/plans/analysis-pipeline-stage-chaining.md,
points 2-3): reads back each of a deal's extracted-document claims
envelopes from Spaces, ingests them into the claims spine under RLS (the
async equivalent of scripts/ingest_claims.py), then runs the verification
chain over the deal's now-ingested claims:

  promote_exact_span (SIM-412)  proposed -> cited, per document
  reconcile_same_fact (3a)      same fact across pages/tiers, per document
  reconcile_consistency (3b)    formula reconstruction, per document
  roll_up_status (SIM-413/254)  cited -> verified|inconclusive|..., per DEAL

That order is not arbitrary. Promotion is a provenance judgment (the cited
span resolved and the binding auditor did not fault it), so it owes nothing
to 3a/3b and runs first. The roll-up is the only step that READS what the
others wrote -- `formula_mismatch` flags and `contradicts` edges are its
internal-disagreement signal -- so it runs last, and deal-wide, because a
`contradicts` edge can point at a claim that arrived in another file.

Runs in the SAQ worker process, same SET LOCAL app.org_id discipline as
app/jobs/tasks/start_deal_analysis.py (see that module's docstring for the
full PgBouncer reasoning). Unlike that task, this one has no external
async wait to poll -- ingest and reconciliation are synchronous DB/Spaces
work, so the whole job runs inside one transaction, committed at the end.

On success it chains into screening (SIM-404), creating that run's row
inside this transaction and enqueuing the job only after the commit -- see
the comment at the hand-off for why the two cannot be swapped.

Scope, decided explicitly (see the stage-chaining doc's "Verification
scope" question): reconcile_same_fact/reconcile_consistency are scoped to
one data_source_id each -- neither does cross-document reconciliation. This
job loops per document, calling both passes once per data_source_id in the
deal. A fact reported in two DIFFERENT documents of the same deal is NOT
caught by this -- that gap is real and open, not solved here.
"""

import asyncio
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from saq.types import Context
from sqlalchemy import select, text

from app.core.database import AsyncSessionLocal
from app.jobs.queue import get_queue
from app.models import Claim, Edge
from app.repo.AnalysisRunRepo import AnalysisRunRepo
from app.repo.DealRepo import DealRepo
from app.repo.HumanAuditRepo import HumanAuditRepo
from app.services.consistency import reconcile_consistency
from app.services.corroboration import (
    CORROBORATABLE_STATUSES,
    CORROBORATION_SOURCES,
    run_corroboration,
)
from app.services.dashboard_structure import merge_dashboard_structures
from app.services.deal_profile import deal_profile_updates
from app.services.qualitative_findings import merge_qualitative_findings
from app.services.reconciliation import reconcile_across_documents, reconcile_same_fact
from app.services.span_promotion import promote_exact_span
from app.services.status_rollup import roll_up_deal
from app.services.uploads.spaces import get_json_object

logger = logging.getLogger(__name__)

# Per-statement / idle-in-transaction ceilings for the verify job's own
# transaction, set well under its SAQ job timeout so a blocked or runaway query
# aborts as a normal error (caught and recorded as `failed` below) instead of
# hanging the job -- and the UI -- for hours. statement_timeout counts lock
# waits too, so a query stuck behind a lock also aborts here.
_STATEMENT_TIMEOUT = "120s"
_IDLE_IN_TXN_TIMEOUT = "120s"

_CONTRACT_PATH = Path(__file__).parents[3] / "contracts" / "claims.schema.json"

# location keys that map to their own flat column -- mirrors
# scripts/ingest_claims.py's _LOCATION_COLUMNS exactly.
_LOCATION_COLUMNS = ("page", "char_start", "char_end", "bbox", "sheet", "cell_ref", "paragraph")


async def _set_org(session, clerk_org_id: str) -> None:
    await session.execute(
        text("SELECT set_config('app.org_id', :tid, true)"),
        {"tid": clerk_org_id},
    )


def _validate_claims(claims: list[dict]) -> None:
    """Same contract check as scripts/ingest_claims.py::_validate -- fail
    loudly before any write if the envelope doesn't match the seam shape."""
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(json.loads(_CONTRACT_PATH.read_text()))
    for i, claim in enumerate(claims):
        errors = sorted(validator.iter_errors(claim), key=str)
        if errors:
            raise ValueError(f"claim {i} violates the contract: {errors[0].message}")


def _row_from_claim(claim: dict, *, org_id: int, deal_id: UUID, data_source_id: UUID) -> Claim:
    """One seam-JSON claim -> one Claim ORM row. Same flattening as
    scripts/ingest_claims.py::_row_from_claim, but deal_id/data_source_id
    are populated here (the demo script leaves both NULL -- it has no deal
    or upload to attribute a claim to)."""
    location = claim["location"]
    row = Claim(
        org_id=org_id,
        deal_id=deal_id,
        data_source_id=data_source_id,
        entity=claim["entity"],
        attribute=claim["attribute"],
        attribute_raw=claim.get("attribute_raw"),
        claim_ref=claim.get("claim_ref"),
        claim_type=claim.get("claim_type", "unknown"),
        value=claim["value"],
        period_year=claim.get("period_year"),
        period_kind=claim.get("period_kind"),
        status=claim["status"],
        verification_method=claim.get("verification_method"),
        section=claim.get("section"),
        flags=claim.get("flags") or None,
        claim_kind=claim.get("claim_kind"),
        assertion_class=claim.get("assertion_class"),
        kind=location["kind"],
    )
    for key in _LOCATION_COLUMNS:
        if key in location:
            setattr(row, key, location[key])
    return row


def _edge_rows_from_envelope(
    envelope: dict, *, org_id: int, ref_to_id: dict[str, UUID], run_id: str
) -> tuple[list[Edge], list[str]]:
    """The parser's own E1-reducer edges (within-page same_fact/contradicts),
    same validation + canonicalization as scripts/ingest_claims.py."""
    from jsonschema import Draft202012Validator

    edge_validator = Draft202012Validator(json.loads(_CONTRACT_PATH.read_text())["$defs"]["edge"])
    rows: list[Edge] = []
    skipped: list[str] = []
    for i, e in enumerate(envelope.get("edges", [])):
        errors = sorted(edge_validator.iter_errors(e), key=str)
        if errors:
            skipped.append(f"edge {i} violates the contract: {errors[0].message}")
            continue
        from_id = ref_to_id.get(e["from"])
        to_id = ref_to_id.get(e["to"])
        if from_id is None or to_id is None:
            missing = ", ".join(
                label for label, got in (("from", from_id), ("to", to_id)) if got is None
            )
            skipped.append(f"{e['type']} {e['from']!r}->{e['to']!r} (missing endpoint: {missing})")
            continue
        # SIM-369: contradicts is symmetric -- canonicalize to from < to so two
        # runs emitting the same pair in opposite order still collapse onto
        # one row under the UNIQUE(org_id, from, to, type). same_fact is
        # directional and is NOT reordered.
        if e["type"] == "contradicts" and from_id > to_id:
            from_id, to_id = to_id, from_id
        rows.append(
            Edge(
                org_id=org_id,
                from_claim_id=from_id,
                to_claim_id=to_id,
                type=e["type"],
                basis=e["basis"],
                created_by="extraction_reducer",
                run_id=run_id,
                metadata_=None,
            )
        )
    return rows, skipped


async def start_deal_verification(
    ctx: Context,
    *,
    analysis_run_id: str,
    parsing_run_id: str,
    clerk_org_id: str,
) -> None:
    """SAQ entrypoint. Thin wrapper whose only job is to guarantee that ANY
    failure of the verification work -- an exception mid-ingest/reconcile, or
    the SAQ timeout cancelling this coroutine -- durably records a terminal
    `failed` status. The work itself runs in one transaction (_run_verification);
    when that transaction rolls back it takes its own `in_progress` marker with
    it, so without this wrapper the run is left non-terminal and the UI renders
    it as "Verifying claims" forever (observed: a multi-hour hang). CancelledError
    is caught alongside Exception because SAQ enforces its job timeout by
    cancelling this coroutine, and CancelledError is a BaseException."""
    run_id = UUID(analysis_run_id)
    try:
        await _run_verification(
            analysis_run_id=analysis_run_id,
            parsing_run_id=parsing_run_id,
            clerk_org_id=clerk_org_id,
        )
    except (Exception, asyncio.CancelledError) as exc:
        await _mark_run_failed(run_id, clerk_org_id, exc)
        raise


async def _run_verification(
    *,
    analysis_run_id: str,
    parsing_run_id: str,
    clerk_org_id: str,
) -> None:
    """`deal_id` is deliberately not a parameter -- org_id/deal_id both come
    from the run rows themselves (get_by_id), so passing it separately would
    just be a second, redundant source of truth for the same value."""
    run_id = UUID(analysis_run_id)

    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"))
        await session.execute(
            text(f"SET LOCAL idle_in_transaction_session_timeout = '{_IDLE_IN_TXN_TIMEOUT}'")
        )
        await _set_org(session, clerk_org_id)
        run_repo = AnalysisRunRepo(session)

        run = await run_repo.get_by_id(run_id)
        if run is None:
            raise ValueError(f"analysis_run {analysis_run_id} not found")

        # Idempotency guard (review on PR #81): the whole job runs in one
        # transaction, so a mid-job crash already rolls back cleanly on its
        # own -- the real exposure is a SAQ redelivery *after* a successful
        # commit, which would otherwise re-run the insert-only ingest below
        # and hit uq_claims_org_data_source_claim_ref on rows already
        # committed, hard-failing the retry and leaving the run stuck. Same
        # spirit as start_deal_analysis's D11 guard: if this run already
        # reached a terminal status, there's nothing left to do.
        if run.status in ("successful", "failed"):
            return
        parsing_run = await run_repo.get_by_id(UUID(parsing_run_id))
        if parsing_run is None:
            raise ValueError(f"analysis_run {parsing_run_id} (parsing) not found")

        org_id, deal_uuid = run.org_id, run.deal_id
        usable_jobs = [job for job in (parsing_run.parse_jobs or []) if job["outcome"] == "parsed"]

        if not usable_jobs:
            await run_repo.update_progress(
                run_id,
                status="failed",
                error_message="No documents were successfully extracted to verify.",
            )
            await HumanAuditRepo(session).append(
                {
                    "org_id": org_id,
                    "actor_id": "Internal System",
                    "actor_email": "Internal System",
                    "event_type": "analysis_verification_completed",
                    "deal_id": deal_uuid,
                    "payload": {"analysis_run_id": analysis_run_id, "status": "failed"},
                }
            )
            return

        await run_repo.update_progress(run_id, status="in_progress")

        job_comments: list[dict] = []
        verified_data_source_ids: list[UUID] = []
        deal_profiles: list[dict | None] = []
        qualitative_findings: list[dict] = []
        dashboard_structures: list[dict | None] = []

        for job in usable_jobs:
            data_source_id = UUID(job["data_source_id"])
            envelope = get_json_object(job["bucket"], job["key"])
            claims = envelope.get("claims", [])
            _validate_claims(claims)
            # Path B: the parser's per-document sector/HQ classification (may be
            # None). Merged after the loop into deal.sector/hq_geography so
            # gs_07/gs_08 have something to screen.
            deal_profiles.append(envelope.get("deal_profile"))
            # Path B "search just in case": per-document grounded verdicts for the
            # selected qualitative (llm) rules. Merged after the loop into
            # deal.qualitative_findings for the document evaluators.
            qualitative_findings.append(envelope.get("qualitative_findings") or {})
            # Pipeline Inspector: the parser's per-document organizing pass (may be
            # None). Merged after the loop into deal.dashboard_structure so the
            # Inspector renders subjects/metric order instead of hardcoding them.
            dashboard_structures.append(envelope.get("dashboard_structure"))

            # ponytail: insert-only, not idempotent against a redelivered/
            # retried job (inherits ingest_claims.py's SIM-367 gap -- a crash
            # mid-ingest followed by a retry will violate
            # uq_claims_org_data_source_claim_ref on the rows already
            # inserted). Fix alongside SIM-367's shared ordered-teardown core
            # if this job's retry behavior ever becomes a real problem.
            rows = [
                _row_from_claim(c, org_id=org_id, deal_id=deal_uuid, data_source_id=data_source_id)
                for c in claims
            ]
            session.add_all(rows)
            await session.flush()

            ref_to_id = {r.claim_ref: r.id for r in rows if r.claim_ref is not None}
            edge_rows, skipped_edges = _edge_rows_from_envelope(
                envelope, org_id=org_id, ref_to_id=ref_to_id, run_id=parsing_run_id
            )
            session.add_all(edge_rows)
            await session.flush()

            verified_data_source_ids.append(data_source_id)
            job_comments.append(
                {
                    "dataSourceId": str(data_source_id),
                    "fileName": job.get("filename"),
                    "status": "ingested",
                    "comment": f"{len(rows)} claim(s) ingested, {len(edge_rows)} edge(s) "
                    f"from extraction, {len(skipped_edges)} edge(s) skipped.",
                }
            )

        # Path B: write the deal's sector/HQ from the documents' classification so
        # gs_07/gs_08 can screen them, and the merged qualitative findings so the
        # document evaluators (gs_01/db_03/...) can. Conservative -- only resolvable
        # dimensions/agreed verdicts are set; a no-op when nothing resolved.
        deal_updates: dict[str, Any] = dict(deal_profile_updates(deal_profiles))
        merged_findings = merge_qualitative_findings(qualitative_findings)
        if merged_findings:
            deal_updates["qualitative_findings"] = merged_findings
        merged_structure = merge_dashboard_structures(dashboard_structures)
        if merged_structure is not None:
            deal_updates["dashboard_structure"] = merged_structure
        if deal_updates:
            await DealRepo(session).update(deal_uuid, deal_updates)

        for data_source_id in verified_data_source_ids:
            # SIM-412 first: the parser leaves every PDF claim at `proposed`,
            # and nothing downstream of here trusts a `proposed` claim
            # (screening's claims_lookup, corroboration's precondition, the
            # status roll-up). Promotion is a provenance judgment -- "the cited
            # span resolved and the binding auditor did not fault it" -- so it
            # is independent of 3a/3b, which decide whether the claim agrees
            # with its neighbours. Running it before them keeps the reading
            # order honest: cite first, then cross-check.
            promoted = await promote_exact_span(session, data_source_id=data_source_id)
            same_fact = await reconcile_same_fact(
                session, data_source_id=data_source_id, run_id=analysis_run_id
            )
            consistency = await reconcile_consistency(
                session, data_source_id=data_source_id, run_id=analysis_run_id
            )
            for comment in job_comments:
                if comment["dataSourceId"] == str(data_source_id):
                    comment["status"] = "verified"
                    comment["comment"] += (
                        f" Promotion: {promoted.claims_promoted} cited via exact_span, "
                        f"{promoted.skipped_binding_unsupported} held at proposed "
                        f"(binding_unsupported). "
                        f"Reconciliation: {same_fact.same_fact_edges} same_fact, "
                        f"{same_fact.contradicts_edges} contradicts. Consistency: "
                        f"{consistency.derived_from_edges} derived_from, "
                        f"{consistency.contradicts_edges} contradicts."
                    )

        # SIM-428: deal-wide cross-DOCUMENT reconciliation, after the
        # per-document passes above and before the roll-up reads their edges.
        # This is the deck-vs-model-vs-data-room internal-consistency check --
        # the handover's alpha priority for pre-seed deals with no registry
        # footprint. It emits the same `contradicts` edges the roll-up already
        # demotes on, so an internally-inconsistent claim drops to `inconclusive`
        # (never `conflicted`, which is reserved for an outside source). DB-only,
        # so unlike the registry adapters it needs no I/O-placement decision.
        cross_document = await reconcile_across_documents(
            session, deal_id=deal_uuid, run_id=analysis_run_id
        )
        logger.info(
            "cross-document reconciliation for deal %s: %s same_fact, %s contradicts "
            "across %s cross-document group(s)",
            deal_uuid,
            cross_document.same_fact_edges,
            cross_document.contradicts_edges,
            cross_document.groups_considered,
        )

        # SIM-413: the status roll-up (SIM-254) last, and deal-scoped rather
        # than per-document. Last because it READS what the passes above wrote
        # -- the `formula_mismatch` flag and `contradicts` edges are its
        # internal-disagreement signal, so running it before them would grade
        # every claim on evidence that did not exist yet. Deal-scoped because
        # trust is a property of the claim, not of the document it arrived in,
        # and a `contradicts` edge can point at a claim from another file.
        #
        # The status filter is the guard, not a try/except: roll_up_status
        # raises ClaimNotCorroboratableError on anything that has not reached
        # an internally-checked status, and `proposed`/`missing`/`rejected`
        # claims are exactly the ones this pass has no verdict for. No
        # `WHERE org_id` -- RLS owns tenant scoping (see CLAUDE.md).
        #
        # The flush is load-bearing, not defensive: AsyncSessionLocal sets
        # autoflush=False, so the promoter's pending `proposed -> cited`
        # mutations are invisible to this SELECT until they hit the DB. Without
        # it the roll-up quietly matches zero claims and the whole step is a
        # no-op that still reports success.
        await session.flush()
        rollup_stmt = (
            select(Claim)
            .where(Claim.deal_id == deal_uuid)
            .where(Claim.status.in_(sorted(CORROBORATABLE_STATUSES)))
        )
        rollup_claims = list((await session.scalars(rollup_stmt)).all())
        # SIM-415: the external corroboration pass runs HERE -- after the claims
        # are cited/reconciled, before the roll-up -- so the roll-up reads the
        # events it writes (agree -> has_agreement, disagree -> conflicted).
        # CORROBORATION_SOURCES is empty until the per-source adapters (SIM-416+)
        # register, so this is a no-op today; the flush below makes any events it
        # does write visible to roll_up_deal's own SELECT (autoflush=False).
        await run_corroboration(session, rollup_claims, CORROBORATION_SOURCES)
        await session.flush()
        # Batched, not one roll_up_status call per claim: on a large deal that
        # was ~2 sequential round-trips per cited claim (the corroboration-event
        # lookup + the contradicts-edge lookup), the round-trip-per-item pattern
        # SIM-411 already had to batch for edge writes. roll_up_deal does the two
        # lookups once for the whole deal.
        await roll_up_deal(session, rollup_claims)
        rollup_counts: Counter[str] = Counter(c.status for c in rollup_claims)
        await session.flush()

        rollup_summary = ", ".join(f"{n} {status}" for status, n in sorted(rollup_counts.items()))
        for comment in job_comments:
            comment["comment"] += f" Status roll-up (deal-wide): {rollup_summary or 'none'}."

        await run_repo.update_progress(run_id, status="successful", job_comments=job_comments)
        await HumanAuditRepo(session).append(
            {
                "org_id": org_id,
                "actor_id": "Internal System",
                "actor_email": "Internal System",
                "event_type": "analysis_verification_completed",
                "deal_id": deal_uuid,
                "payload": {
                    "analysis_run_id": analysis_run_id,
                    "status": "successful",
                    "job_comments": job_comments,
                    "status_rollup": dict(rollup_counts),
                },
            }
        )

        # Chain into screening (SIM-404). The row is created HERE, inside the
        # transaction that just marked this run terminal, for two reasons:
        # uq_analysis_run_active is a partial unique index on deal_id ALONE
        # (not deal_id+job_name), so a screening row can only exist once this
        # run is no longer queued/in_progress -- doing both in one
        # transaction is what makes that legal. And it keeps the chain atomic
        # with the verification result, same pattern as start_deal_analysis's
        # hand-off into this job.
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

    # Outside the transaction -- screening_run_id's row is now durably
    # committed, so a worker that dequeues this immediately is guaranteed to
    # find it. Enqueuing inside the `async with` above let a worker outrace
    # the commit and fail with "analysis_run ... not found" (a real bug hit
    # in local testing on the parsing -> verification hand-off).
    await get_queue().enqueue(
        "start_deal_screening",
        analysis_run_id=str(screening_run_id),
        clerk_org_id=clerk_org_id,
        timeout=3600,
        retries=1,
        ttl=86400,
    )


async def _mark_run_failed(run_id: UUID, clerk_org_id: str, exc: BaseException) -> None:
    """Record a terminal `failed` status for the run in its OWN fresh
    transaction -- the work transaction has already rolled back, so its
    in_progress marker is gone and the run would otherwise sit non-terminal
    (UI hangs on "Verifying claims" indefinitely). Idempotent (skips a run that
    already reached a terminal status, so a SAQ retry after this is a clean
    no-op via _run_verification's own guard) and best-effort: if even this write
    fails it is logged, never raised, so it can never mask the real failure that
    the caller re-raises. error_message carries only the exception TYPE -- never
    str(exc) -- to keep document-derived content out of a persisted field."""
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
                error_message=f"verification failed: {type(exc).__name__}",
            )
            await HumanAuditRepo(session).append(
                {
                    "org_id": run.org_id,
                    "actor_id": "Internal System",
                    "actor_email": "Internal System",
                    "event_type": "analysis_verification_completed",
                    "deal_id": run.deal_id,
                    "payload": {"analysis_run_id": str(run_id), "status": "failed"},
                }
            )
    except Exception:
        logger.exception("could not record verification failure for run %s", run_id)
