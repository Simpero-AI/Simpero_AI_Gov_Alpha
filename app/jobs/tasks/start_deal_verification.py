"""Ingest + verify job (docs/plans/analysis-pipeline-stage-chaining.md,
points 2-3): reads back each of a deal's extracted-document claims
envelopes from Spaces, ingests them into the claims spine under RLS (the
async equivalent of scripts/ingest_claims.py), then runs the two
already-built verification passes -- reconcile_same_fact (3a) and
reconcile_consistency (3b) -- over the deal's now-ingested claims.

Runs in the SAQ worker process, same SET LOCAL app.org_id discipline as
app/jobs/tasks/start_deal_analysis.py (see that module's docstring for the
full PgBouncer reasoning). Unlike that task, this one has no external
async wait to poll -- ingest and reconciliation are synchronous DB/Spaces
work, so the whole job runs inside one transaction, committed at the end.

Scope, decided explicitly (see the stage-chaining doc's "Verification
scope" question): reconcile_same_fact/reconcile_consistency are scoped to
one data_source_id each -- neither does cross-document reconciliation. This
job loops per document, calling both passes once per data_source_id in the
deal. A fact reported in two DIFFERENT documents of the same deal is NOT
caught by this -- that gap is real and open, not solved here.
"""

import json
from pathlib import Path
from uuid import UUID

from saq.types import Context
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.models import Claim, Edge
from app.repo.AnalysisRunRepo import AnalysisRunRepo
from app.repo.HumanAuditRepo import HumanAuditRepo
from app.services.consistency import reconcile_consistency
from app.services.reconciliation import reconcile_same_fact
from app.services.uploads.spaces import get_json_object

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
    """`deal_id` is deliberately not a parameter -- org_id/deal_id both come
    from the run rows themselves (get_by_id), so passing it separately would
    just be a second, redundant source of truth for the same value."""
    run_id = UUID(analysis_run_id)

    async with AsyncSessionLocal() as session, session.begin():
        await _set_org(session, clerk_org_id)
        run_repo = AnalysisRunRepo(session)

        run = await run_repo.get_by_id(run_id)
        if run is None:
            raise ValueError(f"analysis_run {analysis_run_id} not found")
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

        for job in usable_jobs:
            data_source_id = UUID(job["data_source_id"])
            envelope = get_json_object(job["bucket"], job["key"])
            claims = envelope.get("claims", [])
            _validate_claims(claims)

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

        for data_source_id in verified_data_source_ids:
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
                        f" Reconciliation: {same_fact.same_fact_edges} same_fact, "
                        f"{same_fact.contradicts_edges} contradicts. Consistency: "
                        f"{consistency.derived_from_edges} derived_from, "
                        f"{consistency.contradicts_edges} contradicts."
                    )

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
                },
            }
        )
