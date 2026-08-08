"""Value-comparison tolerance -- the shared type-default table.

Comparing two values (product vs golden-set answer key; source vs source in
Pass-2 Verify; a recomputed formula vs its claimed result in 3b consistency)
needs a tolerance: how close counts as a match. Tolerance is a property of the
FIELD TYPE, not of a claim, so it is never a column on `claims` -- the claim
carries `value_type` and the rule is looked up from that here.

Two dimensions: `kind` (relative | absolute | exact) and `value`; the kind
varies by type. Location is never compared here -- a citation resolves to its
exact span or it fails, zero tolerance, and the two never mix.

Two tiers, by design:
  1. Type defaults (TYPE_DEFAULTS below) -- the code-level floor, contract-
     stable, changes ~never.
  2. Per-attribute / per-org overrides -- a `tolerance_overrides` Postgres
     table (id, org_id, attribute, value_type, kind, value, updated_by/at),
     specced but DEFERRED to when per-client deals need tuning: runtime edits
     without a deploy, per-tenant scope, an audit trail. Lookup order once it
     exists: override row (by attribute+org, then value_type+org) -> type
     default. Not built for alpha -- named here so it is not silently missing.
"""

from __future__ import annotations

from typing import Literal

ToleranceKind = Literal["relative", "absolute", "exact"]

# value_type -> (kind, value). Mirrors the contract's value_type enum
# (currency | percent | count | ratio | date | text). `percent` is ABSOLUTE
# (100 bp) because a relative tolerance breaks near 0% and is too loose at high
# values; `currency`/`ratio` are relative because an absolute dollar tolerance
# is meaningless across a $10K and a $10B claim; `count`/`date`/`text` are
# exact -- 47 vs 49 employees is wrong, not close.
#
# `percent` is absolute 1.0: percent normalizes to FACE VALUE (scale.py reads
# "28.5%" -> 28.5, deliberately NOT to basis points), so 1.0 face-value
# percentage point == 100 bp -- the "within 100 bp" the spec intends. (An
# absolute 100 here would be 100 percentage points, i.e. vacuous.)
TYPE_DEFAULTS: dict[str, tuple[ToleranceKind, float]] = {
    "currency": ("relative", 0.05),
    "ratio": ("relative", 0.05),
    "percent": ("absolute", 1.0),
    "count": ("exact", 0.0),
    "date": ("exact", 0.0),
    "text": ("exact", 0.0),
}

# An unknown/absent value_type must not silently widen a comparison: fall back
# to exact so a mistyped type fails closed rather than matching loosely.
_FALLBACK: tuple[ToleranceKind, float] = ("exact", 0.0)


def tolerance_for(value_type: str) -> tuple[ToleranceKind, float]:
    """The (kind, value) tolerance rule for a value_type; exact on unknown."""
    return TYPE_DEFAULTS.get(value_type, _FALLBACK)


def values_match(expected: float, actual: float, value_type: str) -> bool:
    """True if `actual` is within the type-default tolerance of `expected`.

    Numeric comparison only (currency/ratio/percent/count). `date`/`text` map
    to exact here too; string-typed comparisons (the golden-set diff) normalize
    and compare upstream, not through this float path.
    """
    kind, value = tolerance_for(value_type)
    if kind == "exact":
        return expected == actual
    diff = abs(expected - actual)
    if kind == "absolute":
        return diff <= value
    # relative: within `value` of the larger magnitude -- symmetric, and
    # div-by-zero-free without an absolute floor that would swamp small
    # (ratio/percent-scale) values.
    return diff <= value * max(abs(expected), abs(actual))
