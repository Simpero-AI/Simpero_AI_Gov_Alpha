"""Screening #4 (SIM-404): the screening stage, chained after verification.

Reads the deal's now-verified claims + metadata, runs the Track B rulebook
through the decision engine, and writes a cited recommendation to
`screening_result` plus an audit event. Pure DB work -- no external service
to poll, no model calls -- so the whole job runs inside one transaction,
committed at the end, same shape as start_deal_verification.py.

Where the result goes, and why not the "audit log":
`app/models/audit_log.py` is still commented out and `ai_audit_log` has no
writer (and is hash/reference-only by its OD-1 privacy rule, so per-rule
evidence could not go there anyway). `human_audit_log` via HumanAuditRepo is
the working append-only trail this pipeline already writes to, so the event
goes there -- while the queryable record lives in `screening_result`. The
audit row says a screening happened; the result row says what it decided and
on what evidence.

This job never marks a deal declined. `auto_decline` is a RECOMMENDATION
carrying the breaker that fired and its evidence; acting on it is a human's
call, and nothing here writes to `deals.status`.
"""

from uuid import UUID

from saq.types import Context
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.repo.AnalysisRunRepo import AnalysisRunRepo
from app.repo.DealRepo import DealRepo
from app.repo.HumanAuditRepo import HumanAuditRepo
from app.repo.ScreeningResultRepo import ScreeningResultRepo
from app.services.screening.decision import screen_deal


async def _set_org(session, clerk_org_id: str) -> None:
    await session.execute(
        text("SELECT set_config('app.org_id', :tid, true)"),
        {"tid": clerk_org_id},
    )


async def start_deal_screening(
    ctx: Context,
    *,
    analysis_run_id: str,
    clerk_org_id: str,
) -> None:
    """`deal_id` is deliberately not a parameter -- org_id/deal_id both come
    from the run row itself, same reasoning as start_deal_verification."""
    run_id = UUID(analysis_run_id)

    async with AsyncSessionLocal() as session, session.begin():
        await _set_org(session, clerk_org_id)
        run_repo = AnalysisRunRepo(session)

        run = await run_repo.get_by_id(run_id)
        if run is None:
            raise ValueError(f"analysis_run {analysis_run_id} not found")

        # Idempotency guard, same as start_deal_verification's: a SAQ
        # redelivery AFTER a successful commit would otherwise insert a
        # second screening_result row for the same run. Rows here are
        # append-only by design, so nothing would fail loudly -- it would
        # just silently duplicate the record of a decision, which is worse.
        if run.status in ("successful", "failed"):
            return

        org_id, deal_uuid = run.org_id, run.deal_id
        deal = await DealRepo(session).get_by_id(deal_uuid)
        if deal is None:
            await run_repo.update_progress(
                run_id, status="failed", error_message=f"Deal {deal_uuid} not found."
            )
            return

        await run_repo.update_progress(run_id, status="in_progress")

        decision = await screen_deal(session, deal)
        await ScreeningResultRepo(session).record(
            decision, org_id=org_id, deal_id=deal_uuid, analysis_run_id=run_id
        )

        # Deliberately NO job_comments. That column's shape is per-DOCUMENT
        # (JobCommentResponse: dataSourceId/fileName/status/comment) and
        # screening is a deal-level judgment with no document to attribute a
        # verdict to. Writing a deal-level entry there made
        # GET /deals/{id}/status fail response validation outright once a
        # screening row became the deal's latest run. The screening outcome
        # belongs to screening_result and the audit payload below; the
        # status endpoint keeps showing the verification run's comments.
        await run_repo.update_progress(run_id, status="successful")
        await HumanAuditRepo(session).append(
            {
                "org_id": org_id,
                "actor_id": "Internal System",
                "actor_email": "Internal System",
                "event_type": "analysis_screening_completed",
                "deal_id": deal_uuid,
                "payload": {
                    "analysis_run_id": analysis_run_id,
                    "status": "successful",
                    "recommendation": decision.recommendation,
                    "rulebook_version": decision.rulebook_version,
                    "summary": _summarize(decision),
                    # Full per-rule detail, so the append-only trail carries
                    # the evidence independently of the mutable-by-DDL
                    # screening_result table.
                    "rule_results": [r.to_json() for r in decision.results],
                },
            }
        )


def _summarize(decision) -> str:
    """One human-readable line. Names the specific rule on an auto-decline
    and counts the blockers otherwise -- never just "not green", which tells
    a reviewer nothing about what to go look at."""
    if decision.recommendation == "auto_decline":
        triggered = decision.triggered_by
        rule_id = triggered.rule_id if triggered else "unknown rule"
        return f"Auto-decline recommended: deal-breaker {rule_id} matched."
    if decision.recommendation == "green":
        return "Green light: all 11 green signals satisfied, no deal-breaker matched."
    blocking = ", ".join(r.rule_id for r in decision.blocking)
    return f"Human review required: {len(decision.blocking)} unresolved rule(s) -- {blocking}."
