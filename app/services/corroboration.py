"""AE-A-CORR-1 (SIM-252): external-source conflict handling.

Scope is deliberately narrow: react to one outside-source check result
against one claim. Combining this signal with internal verification into a
single overall trust status is AE-A-CORR-2 (status-lifecycle roll-up), not
this module. The internal-document-disagreement half (two documents in the
same deal disagreeing with each other) lives in Human in the Loop (Epic 18),
also not this module.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

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


# The active source registry, consumed by the SIM-416 corroboration job's
# gather phase (start_deal_corroboration) -- NOT inline in the verify
# transaction, so a source's HTTP round-trips never sit inside an open txn.
#
# SEC EDGAR only, for now: it is keyless (public data.sec.gov, descriptive
# User-Agent), needs no resolved entity (matches on claim.entity -> CIK), caches
# one companyfacts fetch per CIK, and is validated against real companyfacts
# shapes. The Corporations Canada / Federal Register / trademark adapters stay
# out until their live endpoints/field-mappings are confirmed and (for the
# entity-lane ones) SIM-420 resolved entities are populated for a deal.
#
# Imported here, below CorroborationSource/CorroborationVerdict, not at module
# top: every adapter imports CorroborationVerdict from this module, so a top
# import would be a cycle.
from app.services.corroboration_sources.sec_edgar import SecEdgarSource  # noqa: E402

CORROBORATION_SOURCES: list[CorroborationSource] = [SecEdgarSource()]


@dataclass(frozen=True)
class GatheredVerdict:
    """One source's verdict on one claim, tagged with the claim id so the write
    phase can re-attach it to a claim re-loaded in a different transaction. The
    gather phase (network I/O, no DB transaction open) collects these; the write
    phase (short transaction) records them. See the two-phase note on
    gather_corroboration."""

    claim_id: uuid.UUID
    outside_source: str
    verdict: CorroborationVerdict


async def gather_corroboration(
    db: AsyncSession,
    claims: Sequence[Claim],
    sources: Sequence[CorroborationSource],
) -> list[GatheredVerdict]:
    """Phase 1 of the corroboration pass: run every source over every
    corroboratable claim and COLLECT the verdicts, writing nothing.

    This is where the network I/O lives, so its caller (the dedicated
    corroboration job, SIM-416) runs it with NO write transaction open: a source
    that holds a socket open for seconds must never hold a Postgres transaction
    open with it. `db` is used read-only here -- sources touch it only to load
    their per-deal context (e.g. the resolved entity), which the caller primes
    before this phase so no query fires mid-gather. `expire_on_commit=False` (see
    app/core/database.py) lets the passed-in `claims` keep their loaded column
    values after the priming transaction has committed, so the sources can read
    claim.value/.entity here without a lazy refresh hitting the closed txn.

    Contracts (unchanged from the single-phase pass):
    - No-signal (source returns None) collects nothing -- absence is never a
      conflict.
    - A source that RAISES is treated as no-signal for that claim, logged, and
      never allowed to fail the pass: one flaky source must not sink a deal's
      corroboration (same durability posture as the verify/screening jobs).
    - Claims outside CORROBORATABLE_STATUSES are skipped."""
    gathered: list[GatheredVerdict] = []
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
            gathered.append(
                GatheredVerdict(
                    claim_id=claim.id,
                    outside_source=source.name,
                    verdict=verdict,
                )
            )
    return gathered


async def apply_corroboration(
    db: AsyncSession,
    claims: Sequence[Claim],
    gathered: Sequence[GatheredVerdict],
) -> None:
    """Phase 2 of the corroboration pass: record the gathered verdicts as
    corroboration events (marking a claim `conflicted` on disagreement), inside
    the caller's write transaction.

    `claims` is the write transaction's OWN re-SELECT of the deal's claims -- the
    gather phase's ORM objects belong to an earlier, now-closed transaction, so
    the mutation record_corroboration_result performs (claim.status) must land on
    a claim attached to this session. A verdict whose claim is no longer present
    (deleted between phases) is skipped. A claim that has since left
    CORROBORATABLE_STATUSES is skipped rather than handed to
    record_corroboration_result (which would raise).

    `db` must already be RLS-scoped by the caller. Does not flush or commit; the
    caller flushes before the roll-up so the events are visible to its SELECT."""
    by_id = {claim.id: claim for claim in claims}
    for item in gathered:
        claim = by_id.get(item.claim_id)
        if claim is None or claim.status not in CORROBORATABLE_STATUSES:
            continue
        await record_corroboration_result(
            db,
            claim=claim,
            outside_source=item.outside_source,
            result=item.verdict.result,
            agrees=item.verdict.agrees,
        )


async def run_corroboration(
    db: AsyncSession,
    claims: Sequence[Claim],
    sources: Sequence[CorroborationSource],
) -> None:
    """Single-transaction corroboration pass: gather then apply against the same
    session. Kept for callers that don't need the network I/O held out of the
    write transaction (and for the existing tests). The dedicated corroboration
    job (SIM-416) instead calls gather_corroboration and apply_corroboration in
    separate transactions, so the source network I/O never sits inside a Postgres
    transaction.

    Meant to run AFTER internal verification (claims at `cited`+) and BEFORE the
    deal-level status roll-up, so the roll-up reads the events this writes. `db`
    must already be RLS-scoped by the caller. Does not flush or commit."""
    gathered = await gather_corroboration(db, claims, sources)
    await apply_corroboration(db, claims, gathered)
