"""SIM-412: the deterministic `proposed -> cited` promotion step, exact-span only.

Nothing promoted a claim out of `proposed` before this. The exact-span
resolver (SIM-59) already finds the span the extractor cited, and the binding
auditor (SIM-359) already flags a span that does not actually support the
value it is attached to -- but the parser emits every PDF claim at `proposed`
and SIM-368's verification passes are edge-and-flag only, deliberately not a
status-promotion engine (app/services/reconciliation.py and
app/services/consistency.py never touch `status`). The result, measured on
staging: `SELECT status, count(*) FROM claims` was 100% `proposed`/`missing`,
zero `cited` -- so screening's trust filter
(app/services/screening/claims_lookup.py) matched nothing and every external
corroborator was blocked, since app/services/corroboration.py refuses a claim
that has not reached `cited`.

This module un-defers the minimum: the claims contract's own
"Verify moves proposed -> cited|rejected" step, for the exact-span method
only. Reranker/prose promotion is NOT here -- that path needs SIM-250's
two-tier enforcement rule, which is parked.

The promotion is a claim about PROVENANCE, not about truth: `cited` means
"the cited span was resolved in the document and the binding auditor did not
fault it", nothing more. Whether the claim agrees with the rest of the
document is 3a/3b's job, and folding that in is the status roll-up's
(app/services/status_rollup.py), which runs after this.

Scope, and what it deliberately leaves alone:

- `missing` claims stay `missing`. A claim with no resolved span has nothing
  to promote on -- ck_claims_missing_has_no_span guarantees they have no
  span, and the span filter below excludes them anyway.
- A `binding_unsupported` claim stays `proposed`. The auditor found the span
  does not support the value; promoting it would launder a known-bad citation
  into a trusted one. Routing it to human review is out of scope here -- it
  simply does not move.
- XLSX claims are not touched: a literal cell is born `cited`/`direct_read`
  ("reading the bytes IS the verification"), and an XLSX claim that is still
  `proposed` is not one an exact TEXT span could vindicate.
- Any claim already past `proposed` is not re-decided. That also makes a
  re-run a no-op -- see the idempotency note on promote_exact_span.

`session` must already be RLS-scoped (SET LOCAL app.org_id) by the caller,
and the caller owns the transaction: this mutates claims in place and never
flushes or commits, same contract as the rest of app/services/.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Claim

# The flag SIM-359's binding auditor writes when a claim's own cited span does
# not support the value attached to it (wrong attribute, collapsed basket,
# absurd scale). Its mode/evidence live in the extractor's flag_log, which this
# app does not persist -- only the flag type survives ingest, which is all this
# pass needs: any binding fault at all is disqualifying.
BINDING_FAULT_FLAG = "binding_unsupported"

# Location kinds whose char_start/char_end are offsets into extracted TEXT, so
# an exact span is a meaningful verification of the citation. XLSX is excluded
# by construction -- see the module docstring.
_TEXT_LOCATION_KINDS = ("pdf", "docx")


@dataclass
class PromotionSummary:
    claims_considered: int = 0
    claims_promoted: int = 0
    skipped_binding_unsupported: int = 0


async def promote_exact_span(
    session: AsyncSession,
    *,
    data_source_id: uuid.UUID | None,
) -> PromotionSummary:
    """Promote this document's span-resolved `proposed` claims to
    `cited`/`exact_span`.

    `data_source_id` narrows to one document, same convention as
    reconcile_same_fact/reconcile_consistency; pass None for the demo ingest
    path, whose claims carry a NULL data_source_id.

    Idempotent by construction rather than by an ON CONFLICT: the selection
    filters on `status == 'proposed'`, so a second run over unchanged claims
    selects nothing and promotes nothing. It is also safe to run after the
    status roll-up has already moved claims on to `verified`/`inconclusive`
    -- those are no longer `proposed`, so this pass cannot drag them back.

    `status` and `verification_method` are set together, in one mutation:
    ck_claims_checked_requires_method rejects a `cited` row whose
    verification_method is NULL, so setting either alone would fail the
    caller's flush.
    """
    stmt = (
        select(Claim)
        .where(Claim.status == "proposed")
        .where(Claim.kind.in_(_TEXT_LOCATION_KINDS))
        # The resolved exact span itself. ck_claims_found_requires_span means
        # nearly every non-missing PDF/DOCX claim already satisfies this, so
        # the real discriminator below is the binding-audit flag, not this --
        # kept explicit anyway so a claim that somehow reaches here without a
        # span can never be promoted on a citation that does not exist.
        .where(Claim.char_start.isnot(None))
        .where(Claim.char_end.isnot(None))
    )
    stmt = stmt.where(
        Claim.data_source_id.is_(None)
        if data_source_id is None
        else Claim.data_source_id == data_source_id
    )
    claims = list((await session.scalars(stmt)).all())

    summary = PromotionSummary(claims_considered=len(claims))
    for claim in claims:
        # In Python rather than a server-side `flags @> ARRAY[...]`: the pass
        # already materialises every candidate, `flags` has no GIN index, and
        # this is the same idiom the other passes read flags with. Note NULL
        # flags -- the common case -- must read as "no fault", which `and`
        # short-circuits correctly.
        if claim.flags and BINDING_FAULT_FLAG in claim.flags:
            summary.skipped_binding_unsupported += 1
            continue

        claim.status = "cited"
        claim.verification_method = "exact_span"
        summary.claims_promoted += 1

    return summary
