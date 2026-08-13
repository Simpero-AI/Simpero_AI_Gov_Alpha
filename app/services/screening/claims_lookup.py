"""Screening #2: shared claim-selection logic every deterministic evaluator
that reads a canonical claim attribute goes through.

These evaluators feed an auto-decline/auto-approve decision -- a higher
trust bar than app/services/reconciliation.py's (which deliberately looks
at every claim, since its job is producing trust signals, not consuming
them). `proposed`/`rejected`/`missing`/`conflicted`/`inconclusive` claims
are treated as if they don't exist here: `conflicted`/`inconclusive` are
themselves "we don't know yet" signals from the reconciliation pass, and
feeding either into a binary screening verdict would silently launder
genuine uncertainty into a false Y/N.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Claim
from app.services.screening.types import ClaimRef

_TRUSTED_STATUSES = ("cited", "partially_verified", "verified")


async def claims_for_attribute(
    session: AsyncSession, deal_id: uuid.UUID, attribute: str
) -> list[Claim]:
    """Every trusted, numeric, non-superseded claim for one (deal, attribute)
    pair. `session` must already be RLS-scoped by the caller, same contract
    as the rest of app/services/."""
    stmt = (
        select(Claim)
        .where(Claim.deal_id == deal_id)
        .where(Claim.attribute == attribute)
        .where(Claim.status.in_(_TRUSTED_STATUSES))
        .where(Claim.value["normalized"].isnot(None))
    )
    claims = list((await session.scalars(stmt)).all())
    # `superseded_by_same_fact`'s own contract meaning is "an edge-ignorant
    # reader should skip this claim, it corroborates the canonical claim
    # elsewhere" -- a screening evaluator is exactly such a reader.
    return [c for c in claims if not (c.flags and "superseded_by_same_fact" in c.flags)]


def select_from(claims: Sequence[Claim], *, target_period_year: int | None = None) -> Claim | None:
    """Pick the one claim an evaluator should read, given several candidates
    (e.g. multiple years of revenue). No target-period concept exists
    upstream yet (that's tickets #5/#6's orchestrator), so the default is
    most-recent-period-wins; a caller that does know its target period can
    still pass one explicitly. Ties (including "no period_year at all") break
    on the lowest claim id, for a fully deterministic pick -- same idiom as
    reconciliation.py's cluster_rank.
    """
    if target_period_year is not None:
        candidates = [c for c in claims if c.period_year == target_period_year]
    else:
        candidates = list(claims)
    if not candidates:
        return None

    years = [c.period_year for c in candidates if c.period_year is not None]
    if years:
        max_year = max(years)
        candidates = [c for c in candidates if c.period_year == max_year]
    return min(candidates, key=lambda c: c.id)


async def select_claim(
    session: AsyncSession,
    deal_id: uuid.UUID,
    attribute: str,
    *,
    target_period_year: int | None = None,
) -> Claim | None:
    claims = await claims_for_attribute(session, deal_id, attribute)
    return select_from(claims, target_period_year=target_period_year)


async def customer_concentration_claim(
    session: AsyncSession, deal_id: uuid.UUID, *, target_period_year: int | None = None
) -> Claim | None:
    """gs_04 and db_07 both key off this one figure -- one lookup, two
    thresholds, each applied by its own evaluator."""
    return await select_claim(
        session, deal_id, "customer_concentration", target_period_year=target_period_year
    )


def share_as_fraction(claim: Claim) -> float | None:
    """A claim's value as a 0-1 fraction, or None if it cannot be converted.

    The parser reads a percent AT FACE VALUE -- "62%" emits
    normalized=62.0, unit="%" (parser_service/scale.py::_self_scaling: a
    percent "carries a unit mark, not a scale mark"). The rulebook's
    thresholds are fractions (max_customer_share_lte: 0.50). Comparing the
    two directly is a units bug with an inverted, auto-declining failure
    mode: a healthy 30% concentration reads as 30.0, which is BOTH `> 0.50`
    (gs_04 -> N, must-have wrongly failed) and `> 0.70` (db_07 -> Y, deal
    wrongly auto-declined). Every unit conversion for a screening threshold
    goes through here rather than being open-coded per evaluator.

    Returns None -- never a guess -- when the unit is one this cannot
    convert, or when the result falls outside 0-1 (a "share" of 620% is a
    mis-scaled figure, not a real concentration). The caller turns that into
    `unknown` with a reason, same fail-closed posture as the rest of this
    module: a figure we cannot read in known units must reach a human, not a
    binary verdict.
    """
    normalized = claim.value.get("normalized")
    if normalized is None:
        return None

    value_type, unit = claim.value.get("value_type"), claim.value.get("unit")
    if value_type == "percent" or unit == "%":
        fraction = normalized / 100.0
    elif value_type == "ratio" or unit == "ratio":
        fraction = normalized
    else:
        return None

    return fraction if 0.0 <= fraction <= 1.0 else None


def claim_ref(claim: Claim) -> ClaimRef:
    return ClaimRef(claim_id=claim.id, attribute=claim.attribute, period_year=claim.period_year)
