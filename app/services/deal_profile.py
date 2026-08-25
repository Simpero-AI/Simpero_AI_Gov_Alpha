"""Map the parser's deal_profile onto the deal's screening fields (Path B).

The parser classifies each document's target sector + HQ against the org's
approved mandate options and returns, per dimension, a fit: "match" (with the
exact approved option), "outside" (a determinable value that fits none of them),
or "unknown". This turns those per-document fits into the `deal.sector` /
`deal.hq_geography` columns that gs_08 / gs_07 read.

Deliberately conservative -- the mapping only ever SETS a column it can resolve:
- match   -> write the approved option verbatim   -> the evaluator returns "met"
- outside -> write the raw sector/HQ (never an approved option) -> "not met"
- unknown / no fit / no signal -> omit the column -> left unchanged -> "review"

So a dimension we can't resolve is left as-is: we never null out an existing
value and never manufacture a false "not met" out of uncertainty. Across several
documents the strongest signal wins (a match beats an outside beats nothing),
since sector/HQ are deal-wide but each document is classified on its own.
"""

from typing import Any

# Strength order for merging per-document reads: a confident approved match wins
# over a determinable non-match; anything weaker contributes nothing.
_RANK = {"match": 0, "outside": 1}


def _candidate(fit: Any, raw: Any) -> tuple[int, str] | None:
    """A single document's (rank, value) for one dimension, or None when it
    carries no usable signal. `fit` is the parser MandateFit dict; `raw` is the
    raw sector/HQ string."""
    if not isinstance(fit, dict):
        return None
    status = fit.get("status")
    if status == "match":
        option = fit.get("option")
        # The parser only emits an on-list option for a match, but never trust a
        # blank one -- a match with no option is not resolvable.
        if isinstance(option, str) and option.strip():
            return (_RANK["match"], option)
        return None
    if status == "outside":
        # Determinable and fits none of the options -> write the raw read so the
        # evaluator returns "not met". No raw value -> nothing to write.
        if isinstance(raw, str) and raw.strip():
            return (_RANK["outside"], raw)
        return None
    return None  # "unknown" (or anything unexpected) -> no signal


def _resolve(profiles: list[dict], fit_key: str, raw_key: str) -> str | None:
    best: tuple[int, str] | None = None
    for profile in profiles:
        candidate = _candidate(profile.get(fit_key), profile.get(raw_key))
        if candidate is not None and (best is None or candidate[0] < best[0]):
            best = candidate
    return best[1] if best is not None else None


def deal_profile_updates(profiles: list[dict | None]) -> dict[str, str]:
    """The `deal` columns to SET from the documents' deal_profile envelopes.

    Only resolvable dimensions appear in the result; pass the returned dict
    straight to DealRepo.update (a no-op on empty). `profiles` may contain None
    (a document with no deal_profile) -- those are ignored."""
    present = [p for p in profiles if isinstance(p, dict)]
    updates: dict[str, str] = {}
    sector = _resolve(present, "sector_fit", "sector")
    if sector is not None:
        updates["sector"] = sector
    hq = _resolve(present, "hq_fit", "hq_geography")
    if hq is not None:
        updates["hq_geography"] = hq
    return updates
