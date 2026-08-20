"""SIM-372: 3b Consistency -- related-fact arithmetic + FinGround formula
reconstruction (design doc Step 3b; arXiv:2604.23588).

Re-executes a computational claim's formula OUTSIDE the parser, from its
operand claims, and compares -- this is what the FinGround paper's 43%
missed-computational-error number is about: a uniform detector that only
checks a claim against its own citation never catches the case where the
citation is byte-exact but the ARITHMETIC connecting it to other claims is
wrong. Routes on `claim_type == "computational"` (SIM-364).

Match -> DERIVED_FROM edges, one row per operand (derived -> operand),
`metadata_={"rule": ..., "operands": [...]}`, `created_by="consistency"`.
Mismatch -> CONTRADICTS edges, one row per operand, plus the
`formula_mismatch` flag on the DERIVED claim (that flag already exists in the
claims contract, reserved for exactly this). Per SIM-372's acceptance: a
mismatch never resolves anything -- every claim involved persists untouched
apart from that one flag.

A claim whose attribute can be checked more than one way (gross_profit =
revenue x margin AND revenue - cogs) is dispositioned as a whole: it earns
DERIVED_FROM only if EVERY evaluable rule matches, and one mismatch makes it
CONTRADICTS-and-flagged with no DERIVED_FROM. So a single claim never carries
both verdicts against the same operand -- the edge graph stays coherent.

HONEST SCOPE, not the full ~15-30 relationships: `DEFAULT_RULES` below is
genuinely rule-driven (`Rule` + `DEFAULT_RULES`) and implements the
fixed-arity, single-entity relationships that fit that shape against the real
`$defs/canonicalAttribute` vocabulary SIM-375 published
(contracts/claims.schema.json) -- revenue x margin = gross profit; revenue -
cogs = gross profit (the same relationship, two independent ways to check
it); ebitda / revenue = ebitda_margin; gross profit - opex = ebitda. No
valuation rule: `pre_money`/`investment`/`post_money` aren't in the 26
`CoreAttribute` names -- they fall into the `operating_metric` escape valve,
which isn't a fixed-arity formula target, so that relationship is dropped
rather than hardcoded against names the vocabulary doesn't have. Deliberately
NOT implemented, and not faked: "segments sum to total" (variable arity,
cross-entity), "two years imply the stated growth" (needs period-pair
matching, not one period), and "table figure = narrative figure modulo
adjustments" (needs a fuzzy-match concept beyond a tolerance).
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Claim
from app.services.edge_writer import flush_edges, stage_edge
from app.services.tolerance import values_match

# Match vs mismatch uses the shared value_type-keyed tolerance table
# (app/services/tolerance.py): currency/ratio relative 5%, percent absolute
# 100 bp, count/date/text exact. The DERIVED claim's value_type selects the
# rule -- a recomputed gross_profit (currency) tolerates 5%, an ebitda_margin
# (percent) tolerates 100 bp, and a ratio no longer gets an absolute floor
# that made sub-1 comparisons vacuous.


@dataclass(frozen=True)
class Rule:
    name: str
    derived_attribute: str
    operand_attributes: tuple[str, ...]
    formula: Callable[[dict[str, float]], float]


# Starter catalog -- see the module docstring for what this covers and what
# it deliberately does not. Every derived_attribute/operand_attributes name
# below must come from contracts/claims.schema.json's $defs/canonicalAttribute
# (SIM-375) -- tests/test_consistency.py asserts this so the two cannot drift.
#
# gross_profit is derived two independent ways (revenue x margin, and
# revenue - cogs) -- SIM-372's guessed vocabulary named these grossProfitUsd
# and grossMarginUsd as if they were different concepts; the real vocabulary
# has one name for both, so a gross_profit claim now gets checked both ways
# whenever both operand sets are present.
DEFAULT_RULES: tuple[Rule, ...] = (
    Rule(
        name="gross_profit_from_revenue_and_margin",
        derived_attribute="gross_profit",
        operand_attributes=("revenue", "gross_margin"),
        formula=lambda o: o["revenue"] * o["gross_margin"],
    ),
    Rule(
        name="ebitda_margin_from_ebitda_and_revenue",
        derived_attribute="ebitda_margin",
        operand_attributes=("ebitda", "revenue"),
        formula=lambda o: o["ebitda"] / o["revenue"] if o["revenue"] else float("nan"),
    ),
    # SIM-373: matches the two subtraction-shaped relationships hand-verified
    # against the cim-01 income statement (see benchmarks/consistency/
    # cim_01.yaml) -- added so that benchmark has at least one real document's
    # data this engine can actually be scored against today, not just
    # illustrative rules.
    Rule(
        name="gross_profit_from_revenue_and_cogs",
        derived_attribute="gross_profit",
        operand_attributes=("revenue", "cogs"),
        formula=lambda o: o["revenue"] - o["cogs"],
    ),
    Rule(
        name="ebitda_from_gross_profit_and_opex",
        derived_attribute="ebitda",
        operand_attributes=("gross_profit", "opex"),
        formula=lambda o: o["gross_profit"] - o["opex"],
    ),
)


def _base(claim: Claim) -> float:
    """An operand's value in a canonical BASE unit for arithmetic: a percent
    (face value 28.5) becomes its ratio (0.285), so `revenue * margin` is
    dimensionally correct whether the margin arrived as "28.5%" (value_type
    percent) or "0.285" (value_type ratio). currency/ratio/count are already
    base. Read from value_type -- never assumed -- so the same rule works
    across documents that express the same field differently."""
    v = float(claim.value["normalized"])
    return v / 100.0 if claim.value.get("value_type") == "percent" else v


def _canonical_from_to(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    return (a, b) if a < b else (b, a)


@dataclass
class ConsistencySummary:
    derived_from_edges: int = 0
    contradicts_edges: int = 0
    claims_flagged: int = 0
    rules_evaluated: int = 0
    skipped_missing_operands: int = 0
    not_implemented: list[str] = field(
        default_factory=lambda: [
            "segments_sum_to_total (variable arity, cross-entity)",
            "two_years_imply_stated_growth (needs period-pair matching)",
            "table_figure_matches_narrative_modulo_adjustments (needs fuzzy match)",
        ]
    )


async def reconcile_consistency(
    session: AsyncSession,
    *,
    data_source_id: uuid.UUID | None,
    session_id: uuid.UUID | None,
    run_id: str,
    rules: Sequence[Rule] = DEFAULT_RULES,
) -> ConsistencySummary:
    """Formula-reconstruction pass over one document's computational claims.

    `session` must already be RLS-scoped by the caller (same contract as
    app/services/reconciliation.py and memory_scope.py). `data_source_id`
    narrows to one document, same reasoning as reconciliation: a formula
    only makes sense within one document's own claims.

    `session_id` (SIM-389) narrows to one ingest run -- see
    reconcile_same_fact's docstring for why that is a separate axis from
    data_source_id and why None (every run at once) is no longer the only
    option. It matters more here than the edge count alone suggests: this
    pass skips any operand key shared by >1 claim as ambiguous, so a second
    run's copy of an operand doesn't just add edges, it SILENTLY SUPPRESSES
    rules that would otherwise evaluate.
    """
    stmt = select(Claim).where(Claim.value["normalized"].isnot(None))
    stmt = stmt.where(
        Claim.data_source_id.is_(None)
        if data_source_id is None
        else Claim.data_source_id == data_source_id
    )
    # Equality only -- a NULL session_id claim belongs to no run. Same
    # reasoning as reconciliation.py.
    if session_id is not None:
        stmt = stmt.where(Claim.session_id == session_id)
    claims = list((await session.scalars(stmt)).all())

    # (entity, period_year, period_kind, attribute) -> claims sharing that key.
    # Ambiguous when >1 claim shares a key (e.g. two conflicting revenue
    # claims for the same period) -- skipped rather than guessing which one
    # is "the" operand; that ambiguity is reconciliation's (SIM-371) job to
    # resolve first, not this pass's to silently pick a side on.
    by_key: dict[tuple[str, int | None, str | None, str], list[Claim]] = {}
    for c in claims:
        by_key.setdefault((c.entity, c.period_year, c.period_kind, c.attribute), []).append(c)

    summary = ConsistencySummary()

    # Group rules by the attribute they derive, so a claim that can be checked
    # more than one way (gross_profit = revenue x margin AND revenue - cogs) is
    # dispositioned ONCE, coherently -- never both derived_from and contradicts.
    rules_by_attribute: dict[str, list[Rule]] = {}
    for rule in rules:
        summary.rules_evaluated += 1
        rules_by_attribute.setdefault(rule.derived_attribute, []).append(rule)

    edges: list[dict] = []
    for attribute, attribute_rules in rules_by_attribute.items():
        derived_candidates = [
            c
            for key, group in by_key.items()
            if key[3] == attribute and len(group) == 1
            for c in group
            if c.claim_type == "computational"
        ]
        for derived in derived_candidates:
            _check_derived(
                derived, attribute_rules, by_key, run_id=run_id, summary=summary, edges=edges
            )
    await flush_edges(session, edges)
    return summary


def _evaluate(
    derived: Claim,
    rule: Rule,
    by_key: dict[tuple[str, int | None, str | None, str], list[Claim]],
    summary: ConsistencySummary,
) -> tuple[dict[str, Claim], float, float] | None:
    """Recompute `rule` for `derived`: (operands, expected, actual) in the
    derived claim's native unit, or None when the rule cannot be evaluated --
    an operand missing/ambiguous, or a non-finite result (a divide-by-zero
    guard yielding nan, e.g. ebitda / revenue with revenue 0). A rule that
    cannot be evaluated is neither a match nor a mismatch and writes nothing."""
    operands: dict[str, Claim] = {}
    for attr in rule.operand_attributes:
        key = (derived.entity, derived.period_year, derived.period_kind, attr)
        group = by_key.get(key)
        if not group or len(group) != 1:
            # Missing, or ambiguous (>1 candidate) -- see the module note on
            # by_key above. Either way, this rule cannot be evaluated here.
            summary.skipped_missing_operands += 1
            return None
        operands[attr] = group[0]

    # Operands in base units; the formula yields a base result, put into the
    # DERIVED claim's storage unit before comparing -- a percent is stored face
    # value (its ratio x 100) -- so the comparison and its value_type tolerance
    # both run in the derived's native units.
    operand_values = {attr: _base(c) for attr, c in operands.items()}
    expected_base = rule.formula(operand_values)
    if not math.isfinite(expected_base):
        summary.skipped_missing_operands += 1
        return None

    derived_vt = derived.value.get("value_type", "")
    expected = expected_base * 100.0 if derived_vt == "percent" else expected_base
    actual = float(derived.value["normalized"])
    return operands, expected, actual


def _check_derived(
    derived: Claim,
    rules: Sequence[Rule],
    by_key: dict[tuple[str, int | None, str | None, str], list[Claim]],
    *,
    run_id: str,
    summary: ConsistencySummary,
    edges: list[dict],
) -> None:
    """Disposition one computational claim against every rule that derives its
    attribute, coherently. It reconstructs cleanly only if EVERY evaluable rule
    matches -- then it earns derived_from edges to those operands. If any
    evaluable rule mismatches, it is flagged formula_mismatch and gets
    contradicts edges to the mismatching rules' operands, and no derived_from
    edges -- so one claim never carries both verdicts. A shared operand
    (revenue, in two rules) yields a single edge."""
    matched: list[tuple[Rule, dict[str, Claim], float, float]] = []
    mismatched: list[tuple[Rule, dict[str, Claim], float, float]] = []
    for rule in rules:
        evaluated = _evaluate(derived, rule, by_key, summary)
        if evaluated is None:
            continue
        operands, expected, actual = evaluated
        derived_vt = derived.value.get("value_type", "")
        bucket = matched if values_match(expected, actual, derived_vt) else mismatched
        bucket.append((rule, operands, expected, actual))

    if mismatched:
        seen_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
        for rule, operands, expected, actual in mismatched:
            for operand in operands.values():
                pair = _canonical_from_to(derived.id, operand.id)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                stage_edge(
                    edges,
                    org_id=derived.org_id,
                    from_claim_id=pair[0],
                    to_claim_id=pair[1],
                    type_="contradicts",
                    basis=f"{rule.name}: recomputed {expected:.4g} vs claimed {actual:.4g}",
                    created_by="consistency",
                    run_id=run_id,
                    metadata_={"rule": rule.name, "value_delta": expected - actual},
                )
                summary.contradicts_edges += 1
        # formula_mismatch: reserved in the claims contract's flags enum for
        # exactly this -- a re-executed formula that disagrees with its own
        # claimed value. Flags the DERIVED claim only; operands are not at fault
        # for a formula that combines them incorrectly.
        if not derived.flags or "formula_mismatch" not in derived.flags:
            derived.flags = [*(derived.flags or []), "formula_mismatch"]
            summary.claims_flagged += 1
        return

    seen_operands: set[uuid.UUID] = set()
    for rule, operands, expected, actual in matched:
        for operand in operands.values():
            if operand.id in seen_operands:
                continue
            seen_operands.add(operand.id)
            stage_edge(
                edges,
                org_id=derived.org_id,
                from_claim_id=derived.id,
                to_claim_id=operand.id,
                type_="derived_from",
                basis=f"{rule.name}: recomputed {expected:.4g} matches claimed {actual:.4g}",
                created_by="consistency",
                run_id=run_id,
                metadata_={"rule": rule.name, "operands": [str(o.id) for o in operands.values()]},
            )
            summary.derived_from_edges += 1
