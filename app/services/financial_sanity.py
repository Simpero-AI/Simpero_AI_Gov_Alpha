"""Deterministic, read-time financial-sanity checks for the Financials tab.

Flags a shown figure that cannot be internally consistent with its
statement-mates -- an accounting identity that fails (revenue - cogs !=
gross_profit), an income-statement ordering that is impossible (ebit > gross
profit), or a magnitude orders of magnitude off its siblings (a scale
mis-detection: a balance-sheet total in thousands next to a revenue in billions).

Pure and LLM-free: computed from the already-extracted numbers alone, so it runs
at READ time over stored claims -- no re-analysis, no Anthropic dependency -- and
never invents a value; it only marks which displayed figures do not add up. It
complements the verify-time consistency pass (SIM-372 `formula_mismatch`), which
only fires when every operand of an EQUALITY was independently extracted in the
same document and each is a single evaluable claim; the ordering and
magnitude shapes here fire from as few as two figures, catching the common
scale/cell-selection breaks that leave the equalities non-evaluable.

Conservative by design (accuracy is paramount): generous slack on every
comparison and a 4-orders-of-magnitude gap before a figure is called
implausibly-scaled, so a real, clean statement is never badged.
"""

from __future__ import annotations

from collections.abc import Mapping

# Value types that carry a dollar magnitude the magnitude check may compare.
# Percents/ratios/counts/text are excluded -- a 46.9% margin beside a $416B
# revenue is not an outlier, it is a different kind of number.
_CURRENCY_TYPES = frozenset({"currency"})

# Relative slack before an identity or ordering counts as violated. Absorbs
# rounding, minor restatement, and sign/threshold noise so a near-equal never
# false-flags; well below the ~10^3 errors this exists to catch.
_REL_SLACK = 0.02

# A currency figure at least this many times below the statement's LARGEST
# currency figure is almost certainly a scale mis-detection (thousands vs
# billions), not a genuine line item -- real statement aggregates sit within ~2-3
# orders of revenue, so 4 orders is a safe floor that clears legitimate small
# lines while catching the "365.0K next to $416B" family.
_MAGNITUDE_RATIO = 1e4
# Below this largest-figure size the statement is too small to magnitude-check
# (a genuine micro-cap), so it is skipped rather than risk a false flag.
_MAGNITUDE_FLOOR = 1e6


def _violates_equality(lhs: float, rhs: float) -> bool:
    scale = max(abs(lhs), abs(rhs))
    return scale > 0 and abs(lhs - rhs) > _REL_SLACK * scale


def _exceeds(smaller: float, larger: float) -> bool:
    """True when `smaller` is materially GREATER than `larger` should allow."""
    return smaller - larger > _REL_SLACK * max(abs(smaller), abs(larger), 1.0)


def flag_implausible(figures: Mapping[str, tuple[float, str]]) -> set[str]:
    """The canonical metric keys among `figures` that cannot be internally
    consistent. `figures` is ONE statement -- a single (entity, period) -- mapping
    a canonical metric key to (normalized_value, value_type). Returns the keys
    implicated in a failed accounting identity, an impossible income-statement
    ordering, or an implausible magnitude; empty when everything reconciles or too
    little is present to judge. Deterministic and side-effect-free."""
    val = {k: v for k, (v, _t) in figures.items()}
    flagged: set[str] = set()

    def has(*keys: str) -> bool:
        return all(k in val for k in keys)

    # --- Accounting identities. A failure implicates the whole relation (any one
    # operand could be the wrong figure), so all its members are flagged -- the
    # FE badge reads "doesn't reconcile", true of the relationship, not a verdict
    # on a single line. cogs/capex are magnitude-only here (sign varies: a deck
    # may carry COGS as -220B or 220B), so abs() keeps the identity sign-robust.
    if has("gross_profit", "revenue", "cogs") and _violates_equality(
        val["gross_profit"], val["revenue"] - abs(val["cogs"])
    ):
        flagged |= {"gross_profit", "revenue", "cogs"}
    if has("total_assets", "total_liabilities", "total_equity") and _violates_equality(
        val["total_assets"], val["total_liabilities"] + val["total_equity"]
    ):
        flagged |= {"total_assets", "total_liabilities", "total_equity"}
    if has("free_cash_flow", "operating_cash_flow", "capex") and _violates_equality(
        val["free_cash_flow"], val["operating_cash_flow"] - abs(val["capex"])
    ):
        flagged |= {"free_cash_flow", "operating_cash_flow", "capex"}
    if has("net_debt", "total_debt", "cash_and_equivalents") and _violates_equality(
        val["net_debt"], val["total_debt"] - val["cash_and_equivalents"]
    ):
        flagged |= {"net_debt", "total_debt", "cash_and_equivalents"}
    if has("working_capital", "current_assets", "current_liabilities") and _violates_equality(
        val["working_capital"], val["current_assets"] - val["current_liabilities"]
    ):
        flagged |= {"working_capital", "current_assets", "current_liabilities"}

    # --- Income-statement ordering. Robust because COGS, operating expenses and
    # D&A are all >= 0: gross profit <= revenue, EBIT <= gross profit, EBIT <=
    # EBITDA. Each violation flags the offending pair.
    if has("gross_profit", "revenue") and _exceeds(val["gross_profit"], val["revenue"]):
        flagged |= {"gross_profit", "revenue"}
    if has("ebit", "gross_profit") and _exceeds(val["ebit"], val["gross_profit"]):
        flagged |= {"ebit", "gross_profit"}
    if has("ebit", "ebitda") and _exceeds(val["ebit"], val["ebitda"]):
        flagged |= {"ebit", "ebitda"}

    # --- Magnitude outliers: a currency figure >= 4 orders below the statement's
    # largest currency figure is a scale mis-detection, not a real line item.
    currency = {k: abs(v) for k, (v, t) in figures.items() if t in _CURRENCY_TYPES and v != 0}
    if len(currency) >= 2:
        ref = max(currency.values())
        if ref >= _MAGNITUDE_FLOOR:
            flagged |= {k for k, mag in currency.items() if mag * _MAGNITUDE_RATIO < ref}

    return flagged
