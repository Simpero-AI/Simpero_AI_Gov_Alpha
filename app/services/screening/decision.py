"""Screening #4: the decision engine -- per-rule verdicts -> one cited
recommendation.

Pure code, no model calls (the same guard test that covers
evaluators/deterministic.py covers this module). The engine owns exactly
three judgments, all of them from the rulebook's own `kind`:

* any `deal_breaker` at Y  -> auto_decline, SHORT-CIRCUIT
* all `green_signal` at Y  -> green
* anything else            -> human_review

It produces a RECOMMENDATION, not a decision. `unknown` never satisfies a
must-have and never auto-declines -- it routes to a human, which is the
whole reason the evaluators are required to return `unknown` rather than
guess.

Ordering note: deal-breakers are evaluated BEFORE green signals, and within
each group in rulebook order. The short-circuit is observable (it is the
difference between one claim lookup and twenty), so the order is part of
this module's contract, not an implementation detail -- see
`ordered_rule_ids`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deal import Deal
from app.services.screening.evaluators.deterministic import EVALUATORS
from app.services.screening.rulebook import Rulebook, load_rulebook
from app.services.screening.types import RuleResult

Recommendation = Literal["auto_decline", "green", "human_review"]

# A rule with no evaluator of its own is out of scope for the deterministic
# CIM-only screener (evaluator: none in track_b.yaml -- SIM-405/406 descoped).
# Its honest answer is `unknown` with a fixed "no evidence" reason: never a
# guess, and it neither satisfies a must-have nor auto-declines. gs_11/db_08
# keep their own structural-unknown note instead (see evaluate_rule).
_NO_EVIDENCE_REASON = "No evidence found in the documents"


@dataclass(frozen=True)
class ScreeningDecision:
    """The full outcome. `results` holds one RuleResult per rule EVALUATED --
    on an auto_decline that is deliberately a partial list, ending at the
    breaker that fired."""

    recommendation: Recommendation
    rulebook_version: str
    results: tuple[RuleResult, ...]
    # The deal_breaker that triggered auto_decline; None otherwise.
    triggered_by: RuleResult | None = None
    # Why this deal is not green: green_signal rules that are not Y, PLUS
    # deal_breaker rules left `unknown` (a breaker we could not rule out is
    # not a cleared breaker). Empty on auto_decline (the run stopped early)
    # and on green.
    blocking: tuple[RuleResult, ...] = field(default_factory=tuple)


def ordered_rule_ids(rulebook: Rulebook) -> list[str]:
    """Deal-breakers first, then green signals, each in rulebook order.

    Breakers first is what makes the short-circuit worth having: a deal that
    operates in a prohibited sector should cost one deal-field read, not a
    full pass over 21 rules including external API calls (#6). It is also the
    cheaper failure to explain to a human -- one cited breaker beats a wall
    of eleven must-have verdicts that no longer matter.
    """
    breakers = [r.id for r in rulebook.rules if r.kind == "deal_breaker"]
    greens = [r.id for r in rulebook.rules if r.kind == "green_signal"]
    return breakers + greens


async def evaluate_rule(
    rule_id: str, session: AsyncSession, deal: Deal, rulebook: Rulebook
) -> RuleResult:
    """One rule's verdict, dispatching to its evaluator -- or an explicit
    `unknown` when the rule's evaluator kind isn't built yet."""
    evaluator = EVALUATORS.get(rule_id)
    if evaluator is not None:
        return await evaluator(session, deal, rulebook)

    rule = rulebook.by_id[rule_id]
    # gs_11/db_08 are unknown BY POLICY -- a negative ("no undisclosed X")
    # that no document can ever prove -- so they keep their own note. Every
    # other out-of-scope rule reports the uniform "No evidence found in the
    # documents".
    reason = rule.unknown or _NO_EVIDENCE_REASON
    # Reported under the rule's OWN evaluator kind ("none"), not
    # "deterministic": the audit trail has to name the real provenance.
    return RuleResult(rule_id, "unknown", None, rule.evaluator, reason=reason)


async def screen_deal(
    session: AsyncSession, deal: Deal, rulebook: Rulebook | None = None
) -> ScreeningDecision:
    """Run the rulebook against one deal and recommend.

    `session` must already be RLS-scoped by the caller (`SET LOCAL
    app.org_id`), same contract as the rest of app/services/.
    """
    rulebook = rulebook or load_rulebook()
    results: list[RuleResult] = []

    for rule_id in ordered_rule_ids(rulebook):
        result = await evaluate_rule(rule_id, session, deal, rulebook)
        results.append(result)

        if rulebook.by_id[rule_id].kind == "deal_breaker" and result.verdict == "Y":
            # Short-circuit: stop evaluating. Everything after this point is
            # work whose answer cannot change the recommendation.
            return ScreeningDecision(
                recommendation="auto_decline",
                rulebook_version=rulebook.version,
                results=tuple(results),
                triggered_by=result,
            )

    # Two different things block green, and BOTH must:
    #   - a green signal that is not Y (the must-have is unmet or unproven)
    #   - a deal-breaker that is `unknown` (we could not rule it out)
    # The second is easy to miss and is the dangerous one. A deal-breaker only
    # counts as cleared when it is a definite N; `unknown` means the check did
    # not run or could not resolve -- #6's OFAC screen timing out, say -- and
    # treating that as "no breaker found" would let a sanctions hit pass as
    # green. Fail closed: an unresolved breaker goes to a human.
    blocking = tuple(
        r
        for r in results
        if (rulebook.by_id[r.rule_id].kind == "green_signal" and r.verdict != "Y")
        or (rulebook.by_id[r.rule_id].kind == "deal_breaker" and r.verdict == "unknown")
    )
    return ScreeningDecision(
        recommendation="green" if not blocking else "human_review",
        rulebook_version=rulebook.version,
        results=tuple(results),
        blocking=blocking,
    )
