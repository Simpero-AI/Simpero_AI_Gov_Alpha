"""AE-A-CORR-1 (SIM-252): external-source conflict handling.

Scope is deliberately narrow: react to one outside-source check result
against one claim. Combining this signal with internal verification into a
single overall trust status is AE-A-CORR-2 (status-lifecycle roll-up), not
this module. The internal-document-disagreement half (two documents in the
same deal disagreeing with each other) lives in Human in the Loop (Epic 18),
also not this module.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.claim import Claim
from app.models.corroboration_event import CorroborationEvent
from app.repo.CorroborationEventRepo import CorroborationEventRepo

# A claim must already have gone through internal verification (reached at
# least `cited`, which requires verification_method to be set — see the
# claims contract) before an outside source has a document-sourced value to
# agree or disagree with. Matches the claims contract's lifecycle note:
# "Verify moves proposed -> cited|rejected; the corroboration pass moves
# cited -> conflicted|inconclusive|partially_verified|verified."
CORROBORATABLE_STATUSES = frozenset(
    {"cited", "conflicted", "inconclusive", "partially_verified", "verified"}
)


class ClaimNotCorroboratableError(ValueError):
    """Raised when a corroboration result targets a claim that hasn't been
    internally checked yet (status not in CORROBORATABLE_STATUSES)."""


async def record_corroboration_result(
    db: AsyncSession,
    *,
    claim: Claim,
    outside_source: str,
    result: dict,
    agrees: bool,
) -> CorroborationEvent:
    """Append an outside-source check to the permanent record, and — when it
    disagrees with the claim's document-sourced value — mark the claim
    `conflicted` rather than silently choosing or averaging (SIM-252
    acceptance criteria).

    `agrees` is decided by the caller, not this function: each outside-source
    check (entity lookup, OFAC screen, USPTO lookup, ...) understands its own
    result shape (a register match, a NOT_FOUND, a CLEAR screen, ...) and
    that domain judgment belongs with the check that produced it, not a
    generic JSON comparator here.

    Never mutates `claim.value` or the event's `result` — the claim's
    document-sourced value and the outside source's finding both stay
    exactly as recorded, so the disagreement itself stays visible (via
    CorroborationEventRepo.list_for_claim) instead of one side quietly
    overwriting the other.

    Raises ClaimNotCorroboratableError, without writing anything, if the
    claim hasn't reached an internally-checked status yet.
    """
    if claim.status not in CORROBORATABLE_STATUSES:
        raise ClaimNotCorroboratableError(
            f"claim {claim.id} has status {claim.status!r}; corroboration requires "
            f"an internally-checked claim (one of {sorted(CORROBORATABLE_STATUSES)})"
        )

    event = await CorroborationEventRepo(db).append(
        {
            "org_id": claim.org_id,
            "claim_id": claim.id,
            "outside_source": outside_source,
            "result": result,
        }
    )

    if not agrees:
        claim.status = "conflicted"

    return event
