"""Document-search evaluators for the qualitative (llm) Track B rules.

These rules -- founder full-time, IP ownership, founder exit intent, cap-table
cleanliness -- have no figure the deterministic engine can compare. Instead the
parser searches the document for each SELECTED one and leaves a grounded finding
on the deal (deals.qualitative_findings, written at verification ingest); this
module turns that finding into a rule verdict.

It makes no model call itself: the AI ran at the parser edge (grounded, with the
evidence quote verified present in the source -- see the parser's
screen_criteria._grounds), and the verdict here is a deterministic read of the
persisted finding. So a Y/N is only ever as strong as a quote that was actually
found in the document; a rule that was not searched (not selected at parse time)
or that the document did not settle reads as unknown -> human review, never a
guess.

A rule is document-searched exactly when track_b.yaml marks it `evaluator: llm`;
the id set below is derived from the rulebook so it cannot drift from the YAML.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deal import Deal
from app.services.screening.rulebook import Rulebook, load_rulebook
from app.services.screening.types import DocumentQuote, RuleResult

# Same string the out-of-scope path uses, so the frontend renders an
# un-settled document rule as "No evidence", exactly like a placeholder rule.
_NO_EVIDENCE_REASON = "No evidence found in the documents"


def _finding_for(deal: Deal, rule_id: str) -> dict | None:
    findings = getattr(deal, "qualitative_findings", None)
    if not isinstance(findings, dict):
        return None
    finding = findings.get(rule_id)
    return finding if isinstance(finding, dict) else None


def _evaluate(rule_id: str, deal: Deal) -> RuleResult:
    finding = _finding_for(deal, rule_id)
    verdict = finding.get("verdict") if finding is not None else None
    if finding is None or verdict not in ("Y", "N"):
        return RuleResult(rule_id, "unknown", None, "llm", reason=_NO_EVIDENCE_REASON)
    return RuleResult(rule_id, verdict, DocumentQuote(finding.get("evidence") or ""), "llm")


def _make(rule_id: str) -> Callable[[AsyncSession, Deal, Rulebook], Awaitable[RuleResult]]:
    async def _evaluator(session: AsyncSession, deal: Deal, rulebook: Rulebook) -> RuleResult:
        return _evaluate(rule_id, deal)

    return _evaluator


# One evaluator per rule the rulebook marks `llm`, built from the rulebook rather
# than hardcoded so adding/removing an `llm` rule in the YAML needs no change here.
DOCUMENT_EVALUATORS: dict[str, Callable[[AsyncSession, Deal, Rulebook], Awaitable[RuleResult]]] = {
    rule.id: _make(rule.id) for rule in load_rulebook().rules if rule.evaluator == "llm"
}
