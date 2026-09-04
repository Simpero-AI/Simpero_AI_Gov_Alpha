"""AE-A-CORR-1 (SIM-252): external-source conflict handling.

Scope is deliberately narrow: react to one outside-source check result
against one claim. Combining this signal with internal verification into a
single overall trust status is AE-A-CORR-2 (status-lifecycle roll-up), not
this module. The internal-document-disagreement half (two documents in the
same deal disagreeing with each other) lives in Human in the Loop (Epic 18),
also not this module.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.claim import Claim
from app.models.corroboration_event import CorroborationEvent
from app.repo.CorroborationEventRepo import CorroborationEventRepo

logger = logging.getLogger(__name__)

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
            "agrees": agrees,
        }
    )

    if not agrees:
        claim.status = "conflicted"

    return event


# AE-A-CORR (Epic 12): the corroboration pass -- run the registered outside
# sources over a deal's internally-checked claims, recording each result via
# record_corroboration_result above. This is the foundation only: it defines the
# source interface and gives record_corroboration_result its first caller. The
# actual sources (SEC EDGAR, Corporations Canada, People Data Labs, ...) are the
# per-adapter tickets (SIM-416+); until they register, CORROBORATION_SOURCES is
# empty and this pass is a no-op, so wiring it into the verify pipeline changes
# no behavior yet.


@dataclass(frozen=True)
class CorroborationVerdict:
    """One outside source's judgment on one claim. `agrees=False` marks the
    claim `conflicted` (SIM-252); `result` is the source's raw finding, kept
    verbatim in the recorded event. A source returns `None` (not a verdict) for
    no-signal -- no record found, or not applicable -- because absence must never
    be read as a conflict (`conflicted` is sticky and would be unrecoverable)."""

    agrees: bool
    result: dict


@runtime_checkable
class CorroborationSource(Protocol):
    """An external corroboration source. Each understands its own domain (a
    register match, an OFAC CLEAR, a Form D hit, ...) and returns a verdict for a
    claim it can speak to, or None for no-signal. `name` is recorded as the
    event's `outside_source`."""

    name: str

    async def check(self, db: AsyncSession, claim: Claim) -> CorroborationVerdict | None: ...


# The active source registry. Empty until the per-source adapters (SIM-416+)
# populate it, so run_corroboration is a no-op in production today -- the
# pipeline seam exists without changing behavior.
CORROBORATION_SOURCES: list[CorroborationSource] = []


async def gather_corroboration(
    db: AsyncSession,
    claims: Sequence[Claim],
    sources: Sequence[CorroborationSource],
) -> list[tuple[UUID, str, CorroborationVerdict]]:
    """Run every source over every corroboratable claim and COLLECT the verdicts,
    writing NOTHING. Split out of run_corroboration so the external HTTP each
    source.check performs can run with NO DB transaction held -- a network call
    pinning the verify transaction is exactly the I/O-placement problem the
    start_deal_corroboration job exists to avoid. Returns (claim_id, source_name,
    verdict) tuples for later persistence.

    Same durability posture as run_corroboration: a source that RAISES is no-signal
    (logged, never fails the pass), a None verdict records nothing, and claims
    outside CORROBORATABLE_STATUSES are skipped. `db` is passed to each check per
    the protocol, but the built-in adapters use it only for the session-memoized
    load_resolved_entity (primed once by the caller), so no query fires here."""
    results: list[tuple[UUID, str, CorroborationVerdict]] = []
    for claim in claims:
        if claim.status not in CORROBORATABLE_STATUSES:
            continue
        for source in sources:
            try:
                verdict = await source.check(db, claim)
            except Exception:
                logger.exception(
                    "corroboration source %r failed for claim %s; treating as no-signal",
                    getattr(source, "name", source),
                    claim.id,
                )
                continue
            if verdict is None:
                continue
            results.append((claim.id, source.name, verdict))
    return results


async def persist_corroboration(
    db: AsyncSession,
    results: Sequence[tuple[UUID, str, CorroborationVerdict]],
    claims_by_id: Mapping[UUID, Claim],
) -> None:
    """Record gathered verdicts (append the event, mark a disagreement conflicted)
    via record_corroboration_result. Runs in the caller's WRITE transaction, AFTER
    the network pass -- no HTTP here. Does not flush or commit; the caller flushes
    before the roll-up so the events are visible to its SELECT (autoflush=False)."""
    for claim_id, outside_source, verdict in results:
        claim = claims_by_id.get(claim_id)
        if claim is None:
            continue
        await record_corroboration_result(
            db,
            claim=claim,
            outside_source=outside_source,
            result=verdict.result,
            agrees=verdict.agrees,
        )


async def run_corroboration(
    db: AsyncSession,
    claims: Sequence[Claim],
    sources: Sequence[CorroborationSource],
) -> None:
    """Gather + persist in one held transaction. Retained for callers/tests that
    run over a small in-memory set; the deal pipeline instead uses gather_corroboration
    (outside any transaction) then persist_corroboration (in a short write txn) so the
    HTTP never sits inside a DB transaction (start_deal_corroboration).

    Contracts (see gather_corroboration/persist_corroboration): no-signal records
    nothing, a raising source is no-signal, claims outside CORROBORATABLE_STATUSES are
    skipped. `db` must already be RLS-scoped; does not flush or commit."""
    results = await gather_corroboration(db, claims, sources)
    await persist_corroboration(db, results, {claim.id: claim for claim in claims})
