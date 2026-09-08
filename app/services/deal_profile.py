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

When a dimension maps to nothing, we still keep the *stated* (grounded raw)
sector/HQ in the display-only `sector_raw` / `hq_geography_raw` columns -- so the
Company Facts box can surface a sector the deck states even when it fits no
approved option (an "unknown" fit). Those columns are never read by a screening
evaluator, so this can never manufacture a false "not met".
"""

from collections.abc import Sequence
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


def _grounded_raw(profiles: list[dict], raw_key: str) -> str | None:
    """The sector/HQ as literally stated in the materials -- the parser's
    grounded raw read, the first non-blank one in document order -- independent
    of any mandate fit. Display-only (deal.sector_raw / deal.hq_geography_raw):
    it lets the Company Facts box show a sector/HQ the deck states even when it
    maps to no approved mandate option. Never fed to a screening evaluator."""
    for profile in profiles:
        raw = profile.get(raw_key)
        if isinstance(raw, str) and raw.strip():
            return raw
    return None


def deal_profile_updates(profiles: Sequence[dict | None]) -> dict[str, str]:
    """The `deal` columns to SET from the documents' deal_profile envelopes.

    Only resolvable dimensions appear in the result; pass the returned dict
    straight to DealRepo.update (a no-op on empty). `profiles` may contain None
    (a document with no deal_profile) -- those are ignored."""
    present = [p for p in profiles if isinstance(p, dict)]
    updates: dict[str, str] = {}
    # Each dimension resolves to EITHER a mandate-mapped screening value OR, when
    # that maps to nothing, the stated (grounded raw) value for display only. The
    # display column mirrors the Company Facts coalesce (`sector or sector_raw`):
    # a sector the deck states still shows even when it fits no approved option.
    # The display column is never read by a screening evaluator.
    sector = _resolve(present, "sector_fit", "sector")
    if sector is not None:
        updates["sector"] = sector
    elif (sector_raw := _grounded_raw(present, "sector")) is not None:
        updates["sector_raw"] = sector_raw
    hq = _resolve(present, "hq_fit", "hq_geography")
    if hq is not None:
        updates["hq_geography"] = hq
    elif (hq_raw := _grounded_raw(present, "hq_geography")) is not None:
        updates["hq_geography_raw"] = hq_raw
    return updates
