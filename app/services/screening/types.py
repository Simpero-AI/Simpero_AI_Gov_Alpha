"""Screening #2: shapes shared across every evaluator kind.

Kept separate from evaluators/deterministic.py so a future llm/external
evaluator (tickets #5/#6) doesn't have to import from a module named
`deterministic`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

Verdict = Literal["Y", "N", "unknown"]


@dataclass(frozen=True)
class ClaimRef:
    claim_id: uuid.UUID
    attribute: str
    period_year: int | None

    def to_json(self) -> dict:
        # `kind` discriminates the evidence union on the way back out of
        # JSONB -- without it a reader has to guess from which keys are
        # present, which stops working the moment either arm gains a field.
        return {
            "kind": "claim",
            "claim_id": str(self.claim_id),
            "attribute": self.attribute,
            "period_year": self.period_year,
        }


@dataclass(frozen=True)
class DealField:
    field: str
    value: str | float | None

    def to_json(self) -> dict:
        return {"kind": "deal_field", "field": self.field, "value": self.value}


@dataclass(frozen=True)
class DocumentQuote:
    """A verbatim sentence from the parsed document that backs a qualitative
    (llm) verdict -- e.g. gs_01/db_03's grounded "search just in case". The quote
    was verified present in the source before it reached here (parser
    screen_criteria._grounds), so it is a real span a human can locate, not a
    model paraphrase."""

    quote: str

    def to_json(self) -> dict:
        return {"kind": "document", "quote": self.quote}


EvaluatorKind = Literal["deterministic", "llm", "external", "hybrid", "none"]


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    verdict: Verdict
    # What backed a Y/N verdict. None on `unknown` -- there's nothing to
    # point at.
    evidence: ClaimRef | DealField | DocumentQuote | None
    # Which KIND of evaluator produced this verdict. Widened past
    # "deterministic" for #4's benefit: when a rule has no evaluator built
    # yet, the engine still emits a RuleResult for it, and labelling that
    # `deterministic` would misreport an LLM rule's provenance in the audit
    # trail -- the one thing this field exists to get right.
    evaluator: EvaluatorKind
    # Free-text explanation, populated on `unknown` (e.g. "no revenue claim
    # on the deal") and on the two hybrid gates' deferred-to-LLM case. Kept
    # separate from `evidence`: evidence is a structured reference, reason
    # is prose, and conflating them would corrupt evidence's typed union.
    reason: str | None = None
    # 1.0 for a deterministic Y/N: the comparison either held or it didn't,
    # there is no model uncertainty to report. #5's LLM rules are what make
    # this field carry real intermediate values.
    confidence: float = 1.0

    def __post_init__(self) -> None:
        # An `unknown` is the ABSENCE of a verdict, so there is nothing to be
        # confident about -- 0.0, always, whatever the caller passed. Enforced
        # here rather than left to ~20 construction sites because the failure
        # mode is silent: an `unknown` carrying the default 1.0 into the audit
        # trail reads as "we are certain", the exact opposite of what it means.
        if self.verdict == "unknown" and self.confidence != 0.0:
            object.__setattr__(self, "confidence", 0.0)

    def to_json(self) -> dict:
        """The persisted shape (screening_result.rule_results) and the API
        shape are the same object -- #4's acceptance names verdict,
        evaluator, evidence_ref and confidence, so all four are always
        present, `evidence_ref` explicitly null rather than omitted when
        there is nothing to cite."""
        return {
            "rule_id": self.rule_id,
            "verdict": self.verdict,
            "evaluator": self.evaluator,
            "evidence_ref": self.evidence.to_json() if self.evidence is not None else None,
            "confidence": self.confidence,
            "reason": self.reason,
        }
