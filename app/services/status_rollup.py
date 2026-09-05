"""AE-A-CORR-2 (SIM-254): status-lifecycle roll-up.

Combines three signals into one final trust status for a claim, replacing
the retired "triangulation" concept:

1. Document-sourced statement — implicit: the claim already reached
   `cited` (Verify's `proposed -> cited|rejected` step already ran).
2. Internal verification strength — `verification_method`, demoted by
   internal cross-claim disagreement. exact_span, formula_reexecution,
   direct_read, and human_review are byte-exact or explicitly
   human-confirmed, so they're treated as dispositive on their own
   (matches the claims contract's own framing: "reading the bytes IS the
   verification" for a direct XLSX read). reranker is a semantic/fuzzy
   match — weaker on its own, and needs external agreement to earn full
   trust. Either way, a claim the verification passes themselves flagged
   as internally inconsistent never counts as strong — see
   has_internal_disagreement below.
3. External corroboration — derived from `corroboration_events` plus the
   claim's current status. SIM-252's `conflicted` transition is sticky (a
   later agreeing event never clears it — see app/services/corroboration.py
   test_agreeing_event_does_not_clear_existing_conflict), which is what
   lets this module treat `status == 'conflicted'` as "a disagreement
   happened, ever" without needing an agrees flag stored per event.

   That inference is load-bearing and fragile: it holds only while
   `record_corroboration_result` is the sole writer of corroboration
   events. `CorroborationEventRepo.append()` is public, and a second
   caller that appends a disagreeing event without setting the claim
   `conflicted` would make this module read that disagreement as an
   agreement. If such a caller is ever added, persist the verdict on the
   event instead of inferring it here.

Internal disagreement DEMOTES rather than producing `conflicted`.
`conflicted` means "an outside source disagreed" — SIM-252's meaning, and
app/services/corroboration.py hands internal-document-vs-document conflict
to Human in the Loop (Epic 18) explicitly. Minting `conflicted` from an
internal signal would collide with both. Demotion keeps the vocabulary
honest, stays out of Epic 18's lane, and still guarantees that a claim
whose arithmetic failed re-execution can never reach `verified` on the
strength of its citation alone.

Single source of truth: other features (Background Check Card,
Triangulation view UI) read `claims.status` after this runs, rather than
re-deriving their own status.

Nothing calls this in production yet, deliberately — same as SIM-252's
record_corroboration_result, which also shipped with no caller. Deciding
where in the pipeline the roll-up runs is DS-A-CORR-1 (SIM-253), and it is
a real product change: app/services/screening/claims_lookup.py trusts
`cited` but not `inconclusive`, so a reranker-verified claim with no
external check drops out of screening the moment this runs over it (pinned
by test_rollup_demotes_a_claim_out_of_screening_trust).

`db` must already be RLS-scoped by the caller (`SET LOCAL app.org_id`),
same contract as the rest of app/services/.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.claim import Claim
from app.models.corroboration_event import CorroborationEvent
from app.models.edge import Edge
from app.repo.CorroborationEventRepo import CorroborationEventRepo
from app.services.corroboration import (
    CORROBORATABLE_STATUSES,
    PRESENCE_ONLY_SOURCES,
    ClaimNotCorroboratableError,
)

# Exhaustive over the verification_method enum (ck_claims_verification_method):
# exact_span, formula_reexecution, direct_read, reranker, human_review.
# Anything not in this set (in practice, only reranker) is treated as weak —
# the conservative default for a financial-diligence platform.
STRONG_VERIFICATION_METHODS = frozenset(
    {"exact_span", "formula_reexecution", "direct_read", "human_review"}
)

RESOLVED_STATUSES = frozenset({"verified", "partially_verified", "conflicted", "inconclusive"})


def resolve_status(
    *,
    verification_method: str,
    internal_disagreement: bool,
    has_disagreement: bool,
    has_agreement: bool,
) -> str:
    """Pure roll-up rule — see module docstring for the three signals.

    internal_disagreement: one of this deal's own verification passes found
    this claim inconsistent with its neighbours (3b's formula_mismatch, or a
    `contradicts` edge from 3a/3b/the parser's reducer).
    has_disagreement: an external source has ever disagreed with this claim.
    has_agreement: at least one external check has run and none disagreed
    (only consulted when has_disagreement is False).
    """
    if has_disagreement:
        return "conflicted"

    if verification_method in STRONG_VERIFICATION_METHODS and not internal_disagreement:
        return "verified"

    return "partially_verified" if has_agreement else "inconclusive"


async def has_internal_disagreement(db: AsyncSession, claim: Claim) -> bool:
    """Whether this deal's own verification passes found the claim
    inconsistent with its neighbours.

    Two signals, both written by passes that deliberately never touch
    `status` themselves (app/services/consistency.py,
    app/services/reconciliation.py) — reading them is this module's job:

    - the `formula_mismatch` flag: 3b re-executed the claim's formula from
      its operands and got a different answer.
    - a `contradicts` edge touching the claim, from any writer.

    Both edge directions are checked: reconciliation canonicalizes
    `contradicts` as from < to, while consistency writes derived -> operand,
    so a one-sided lookup would miss half of them.

    `superseded_by_same_fact` is deliberately NOT a disagreement — per its
    contract meaning the claim AGREES with a canonical claim elsewhere, so
    demoting on it would penalise corroboration.
    """
    if claim.flags and "formula_mismatch" in claim.flags:
        return True

    stmt = (
        select(Edge.id)
        .where(Edge.type == "contradicts")
        .where(or_(Edge.from_claim_id == claim.id, Edge.to_claim_id == claim.id))
        .limit(1)
    )
    return (await db.scalars(stmt)).first() is not None


async def roll_up_status(db: AsyncSession, claim: Claim) -> str:
    """Recompute and persist the claim's rolled-up status. Idempotent and
    safe to call again as new corroboration events arrive.

    Mutates `claim.status` in the caller's transaction — no flush, no commit,
    same contract as the rest of app/services/."""
    if claim.status not in CORROBORATABLE_STATUSES:
        raise ClaimNotCorroboratableError(
            f"claim {claim.id} has status {claim.status!r}; status roll-up requires "
            f"an internally-checked claim (one of {sorted(CORROBORATABLE_STATUSES)})"
        )
    if claim.verification_method is None:
        # Guarded by ck_claims_checked_requires_method at the DB level for
        # every status in CORROBORATABLE_STATUSES — unreachable in practice.
        # Kept as an explicit failure rather than a confusing downstream bug
        # if that invariant is ever broken upstream.
        raise ValueError(f"claim {claim.id} has status {claim.status!r} but no verification_method")

    has_disagreement = claim.status == "conflicted"
    has_agreement = False
    if not has_disagreement:
        events = await CorroborationEventRepo(db).list_for_claim(claim.id)
        # Display-only sources (web-search) do not count as a corroboration
        # agreement -- see PRESENCE_ONLY_SOURCES and roll_up_deal's group query.
        has_agreement = any(e.outside_source not in PRESENCE_ONLY_SOURCES for e in events)

    claim.status = resolve_status(
        verification_method=claim.verification_method,
        internal_disagreement=await has_internal_disagreement(db, claim),
        has_disagreement=has_disagreement,
        has_agreement=has_agreement,
    )
    return claim.status


async def roll_up_deal(db: AsyncSession, claims: Sequence[Claim]) -> None:
    """Batched equivalent of calling roll_up_status on every claim: two
    set-building queries for the whole group -- the `contradicts` edges and the
    corroboration events -- instead of the two queries per claim that
    roll_up_status issues, then resolve_status in memory. Same result as the
    per-claim path; avoids the round-trip-per-claim cost when a deal has many
    cited claims (the pattern batched for edge writes in SIM-411). Mutates each
    claim.status in place; no flush, no commit.

    Every claim must already be in CORROBORATABLE_STATUSES and carry a
    verification_method -- the same preconditions roll_up_status enforces (the
    caller's `status IN CORROBORATABLE_STATUSES` filter provides the first, the
    ck_claims_checked_requires_method CHECK the second)."""
    claim_ids = [c.id for c in claims]
    if not claim_ids:
        return

    # Claims on either end of a `contradicts` edge -- the same signal
    # has_internal_disagreement reads, one query for the whole group. Both ends
    # are collected; ids outside this group are harmless since only these
    # claims are looked up.
    edge_rows = (
        await db.execute(
            select(Edge.from_claim_id, Edge.to_claim_id)
            .where(Edge.type == "contradicts")
            .where(or_(Edge.from_claim_id.in_(claim_ids), Edge.to_claim_id.in_(claim_ids)))
        )
    ).all()
    contradicted: set[uuid.UUID] = set()
    for from_id, to_id in edge_rows:
        contradicted.add(from_id)
        contradicted.add(to_id)

    # Claims with at least one TRUST-BEARING corroboration event -- one query for
    # the group, matching roll_up_status's `len(list_for_claim(...)) > 0`.
    # PRESENCE_ONLY_SOURCES (the display-only web-search source) are excluded: their
    # events must not count as a corroboration agreement, or a non-deterministic web
    # match would promote an uncorroborated claim into _TRUSTED on the NEXT run's
    # roll-up (the events persist append-only across re-analysis).
    claims_with_events: set[uuid.UUID] = set(
        (
            await db.scalars(
                select(CorroborationEvent.claim_id)
                .where(CorroborationEvent.claim_id.in_(claim_ids))
                .where(CorroborationEvent.outside_source.not_in(sorted(PRESENCE_ONLY_SOURCES)))
            )
        ).all()
    )

    for claim in claims:
        if claim.status not in CORROBORATABLE_STATUSES:
            raise ClaimNotCorroboratableError(
                f"claim {claim.id} has status {claim.status!r}; status roll-up requires "
                f"an internally-checked claim (one of {sorted(CORROBORATABLE_STATUSES)})"
            )
        if claim.verification_method is None:
            raise ValueError(
                f"claim {claim.id} has status {claim.status!r} but no verification_method"
            )
        has_disagreement = claim.status == "conflicted"
        internal_disagreement = (
            bool(claim.flags) and "formula_mismatch" in claim.flags
        ) or claim.id in contradicted
        claim.status = resolve_status(
            verification_method=claim.verification_method,
            internal_disagreement=internal_disagreement,
            has_disagreement=has_disagreement,
            has_agreement=(not has_disagreement) and claim.id in claims_with_events,
        )
