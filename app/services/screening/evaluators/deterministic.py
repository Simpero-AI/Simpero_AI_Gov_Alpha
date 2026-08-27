"""Screening #2: pure-code evaluators for the deterministic Track B rules.

No model calls anywhere in this module -- deterministic means deterministic
(see tests/test_screening_no_llm_client.py's guard). Thresholds always come
from the loaded rulebook (#1), never hardcoded here -- including db_04's
prohibited-sector list, which already lives in track_b.yaml's own
`threshold.in`; duplicating it in Python would create two sources of truth
for the same policy. Python only owns the comparison logic YAML can't
express (>, <=, "is in", the runway division, ...).

`unknown` is mandatory whenever a required figure is absent -- never a
default guess at Y or N.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deal import Deal
from app.services.screening.claims_lookup import (
    claim_ref,
    customer_concentration_claim,
    select_claim,
    share_as_fraction,
)
from app.services.screening.evaluators.document import DOCUMENT_EVALUATORS
from app.services.screening.rulebook import Rulebook
from app.services.screening.types import DealField, RuleResult
from app.services.screening.workspace_config import load_workspace_config


async def evaluate_gs_03(session: AsyncSession, deal: Deal, rulebook: Rulebook) -> RuleResult:
    """Company has a paying customer (any revenue)."""
    rule = rulebook.by_id["gs_03"]
    assert rule.threshold is not None  # gs_03 always carries a threshold in track_b.yaml
    claim = await select_claim(session, deal.id, "revenue")
    if claim is None:
        return RuleResult(
            "gs_03", "unknown", None, "deterministic", reason="no revenue claim on the deal"
        )
    verdict = "Y" if claim.value["normalized"] > rule.threshold["revenue_gt"] else "N"
    return RuleResult("gs_03", verdict, claim_ref(claim), "deterministic")


async def evaluate_gs_04(session: AsyncSession, deal: Deal, rulebook: Rulebook) -> RuleResult:
    """No single customer >50% of revenue."""
    rule = rulebook.by_id["gs_04"]
    assert rule.threshold is not None
    claim = await customer_concentration_claim(session, deal.id)
    if claim is None:
        return RuleResult(
            "gs_04",
            "unknown",
            None,
            "deterministic",
            reason="customer_concentration not extracted",
        )
    share = share_as_fraction(claim)
    if share is None:
        return RuleResult(
            "gs_04",
            "unknown",
            claim_ref(claim),
            "deterministic",
            reason=(
                "customer_concentration is not readable as a 0-1 share "
                f"(value={claim.value.get('normalized')!r}, "
                f"unit={claim.value.get('unit')!r})"
            ),
        )
    verdict = "Y" if share <= rule.threshold["max_customer_share_lte"] else "N"
    return RuleResult("gs_04", verdict, claim_ref(claim), "deterministic")


async def evaluate_db_07(session: AsyncSession, deal: Deal, rulebook: Rulebook) -> RuleResult:
    """Single customer >70% of revenue (deal-breaker). Reads the same
    customer_concentration figure as gs_04, its own threshold."""
    rule = rulebook.by_id["db_07"]
    assert rule.threshold is not None
    claim = await customer_concentration_claim(session, deal.id)
    if claim is None:
        return RuleResult(
            "db_07",
            "unknown",
            None,
            "deterministic",
            reason="customer_concentration not extracted",
        )
    share = share_as_fraction(claim)
    if share is None:
        # Deliberately `unknown`, not N: this is the auto-decline path, and a
        # figure we cannot read in known units must reach a human rather than
        # silently clearing the deal-breaker.
        return RuleResult(
            "db_07",
            "unknown",
            claim_ref(claim),
            "deterministic",
            reason=(
                "customer_concentration is not readable as a 0-1 share "
                f"(value={claim.value.get('normalized')!r}, "
                f"unit={claim.value.get('unit')!r})"
            ),
        )
    verdict = "Y" if share > rule.threshold["max_customer_share_gt"] else "N"
    return RuleResult("db_07", verdict, claim_ref(claim), "deterministic")


async def evaluate_gs_07(session: AsyncSession, deal: Deal, rulebook: Rulebook) -> RuleResult:
    """HQ in approved geography."""
    if deal.hq_geography is None:
        return RuleResult(
            "gs_07", "unknown", None, "deterministic", reason="hq_geography not set on the deal"
        )
    config = await load_workspace_config(session)
    if config.approved_geographies is None:
        return RuleResult(
            "gs_07",
            "unknown",
            None,
            "deterministic",
            reason="no approved geographies in the org's mandate",
        )
    # approves_geography(), not `in`: matching folds case/whitespace on both
    # sides (workspace_config.normalize_label). The evidence below stays the
    # RAW deal string -- what the deal actually says, not the folded key.
    verdict = "Y" if config.approves_geography(deal.hq_geography) else "N"
    return RuleResult(
        "gs_07", verdict, DealField("hq_geography", deal.hq_geography), "deterministic"
    )


async def evaluate_gs_08(session: AsyncSession, deal: Deal, rulebook: Rulebook) -> RuleResult:
    """Operates in approved sector."""
    if deal.sector is None:
        return RuleResult(
            "gs_08", "unknown", None, "deterministic", reason="sector not set on the deal"
        )
    config = await load_workspace_config(session)
    if config.approved_sectors is None:
        return RuleResult(
            "gs_08",
            "unknown",
            None,
            "deterministic",
            reason="no approved sectors in the org's mandate",
        )
    verdict = "Y" if config.approves_sector(deal.sector) else "N"
    return RuleResult("gs_08", verdict, DealField("sector", deal.sector), "deterministic")


async def evaluate_db_04(session: AsyncSession, deal: Deal, rulebook: Rulebook) -> RuleResult:
    """Sector is prohibited (cannabis/gambling/crypto-native/defense
    manufacturing). The prohibited list is fixed/global -- read straight off
    the rulebook's own threshold, not workspace config (unlike gs_08's
    approved list, which is genuinely per-org)."""
    rule = rulebook.by_id["db_04"]
    assert rule.threshold is not None
    if deal.sector is None:
        return RuleResult(
            "db_04", "unknown", None, "deterministic", reason="sector not set on the deal"
        )
    verdict = "Y" if deal.sector in rule.threshold["in"] else "N"
    return RuleResult("db_04", verdict, DealField("sector", deal.sector), "deterministic")


async def evaluate_db_01_gate(session: AsyncSession, deal: Deal, rulebook: Rulebook) -> RuleResult:
    """No paying customers and no signed LOIs/pilots -- deterministic gate
    only. This evaluator never returns Y: the second clause (signed
    LOIs/pilots) is not assessable from the CIM. revenue > 0 clears the gate
    (N, no breaker); revenue == 0 leaves the rule `unknown` -- no evidence in
    the documents for the LOI/pilot clause -- distinguished by its reason
    from the missing-claim case."""
    rule = rulebook.by_id["db_01"]
    assert rule.threshold is not None
    claim = await select_claim(session, deal.id, "revenue")
    if claim is None:
        return RuleResult(
            "db_01", "unknown", None, "deterministic", reason="no revenue claim on the deal"
        )
    if claim.value["normalized"] > rule.threshold["revenue_eq"]:
        return RuleResult("db_01", "N", claim_ref(claim), "deterministic")
    return RuleResult(
        "db_01",
        "unknown",
        claim_ref(claim),
        "deterministic",
        reason=(
            "revenue == 0; the no-LOI/pilot clause is not assessable from "
            "the CIM -- no evidence in the documents"
        ),
    )


async def evaluate_db_02_gate(session: AsyncSession, deal: Deal, rulebook: Rulebook) -> RuleResult:
    """Runway <6 months with no active raise -- deterministic gate only.
    runway = cash_and_equivalents / monthly_burn, computed here (not an
    extracted figure). Both claims must resolve, and burn must be nonzero,
    or the result is `unknown` -- a zero burn is a data-quality case this
    evaluator can't respectably resolve either way, not a real green signal.
    runway >= 6mo clears the gate (N, no breaker); runway < 6mo leaves the
    rule `unknown` -- the no-active-raise clause is not assessable from the
    CIM -- and stays `unknown` here."""
    rule = rulebook.by_id["db_02"]
    assert rule.threshold is not None
    cash = await select_claim(session, deal.id, "cash_and_equivalents")
    burn = await select_claim(session, deal.id, "monthly_burn")
    if cash is None or burn is None:
        missing = [
            name
            for name, claim in (("cash_and_equivalents", cash), ("monthly_burn", burn))
            if claim is None
        ]
        return RuleResult(
            "db_02",
            "unknown",
            None,
            "deterministic",
            reason=f"{' and '.join(missing)} claim not available",
        )
    burn_value = burn.value["normalized"]
    if burn_value == 0:
        return RuleResult(
            "db_02",
            "unknown",
            claim_ref(burn),
            "deterministic",
            reason="monthly_burn is zero -- runway undefined",
        )
    runway_months = cash.value["normalized"] / burn_value
    if runway_months >= rule.threshold["runway_months_lt"]:
        return RuleResult("db_02", "N", claim_ref(cash), "deterministic")
    return RuleResult(
        "db_02",
        "unknown",
        claim_ref(cash),
        "deterministic",
        reason=(
            "runway < 6 months; the no-active-raise clause is not assessable "
            "from the CIM -- no evidence in the documents"
        ),
    )


EVALUATORS: dict[str, Callable[[AsyncSession, Deal, Rulebook], Awaitable[RuleResult]]] = {
    "gs_03": evaluate_gs_03,
    "gs_04": evaluate_gs_04,
    "gs_07": evaluate_gs_07,
    "gs_08": evaluate_gs_08,
    "db_01": evaluate_db_01_gate,
    "db_02": evaluate_db_02_gate,
    "db_04": evaluate_db_04,
    "db_07": evaluate_db_07,
    # Document-search (llm) rules read the parser's grounded finding off the deal;
    # they make no model call here (see evaluators/document.py). Merged in so the
    # dispatcher (decision.evaluate_rule) resolves them like any other evaluator.
    **DOCUMENT_EVALUATORS,
}
