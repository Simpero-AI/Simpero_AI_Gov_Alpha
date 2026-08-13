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


@dataclass(frozen=True)
class DealField:
    field: str
    value: str | float | None


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    verdict: Verdict
    # What backed a Y/N verdict. None on `unknown` -- there's nothing to
    # point at.
    evidence: ClaimRef | DealField | None
    evaluator: Literal["deterministic"]
    # Free-text explanation, populated on `unknown` (e.g. "no revenue claim
    # on the deal") and on the two hybrid gates' deferred-to-LLM case. Kept
    # separate from `evidence`: evidence is a structured reference, reason
    # is prose, and conflating them would corrupt evidence's typed union.
    reason: str | None = None
