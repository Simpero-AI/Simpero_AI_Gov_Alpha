"""Web-search deep-search CORROBORATE pass (Epic 12 / SIM-419, Slice 2).

Confirms the deck's OWN market-sizing figures (TAM/SAM/SOM/market size) against
the reputable web figures the COLLECT pass already gathered for the same metric,
so the Corroboration tab can show "the deck's $5B TAM is corroborated by Grand
View Research ($6B)" and cite the page. It reuses the SINGLE Anthropic web_search
call gather_web_facts runs -- there is NO per-claim search -- and does a pure,
deterministic value comparison.

POSITIVE-ONLY, DISPLAY-ONLY -- and deliberately so. This pass only ever emits
`agrees=True` for a deck figure a web source confirms within a generous band; it
never emits a conflict and never changes a claim's trust status. Two hard reasons,
both learned from wiring a fuzzy source into the corroboration engine:

  1. `conflicted` is STICKY and unrecoverable (SIM-252): record_corroboration_result
     only ever WRITES conflicted, the roll-up re-derives it from the claim's own
     status, and corroboration_events are append-only -- so a corrected re-run can
     never clear it. And web_search input is NON-DETERMINISTIC run-to-run, so one
     outlier figure would permanently sink a legitimate deck figure below _TRUSTED
     and drop it from the Market tab. A false conflict is the worst outcome here.
  2. Even a benign agreement must not run the deal roll-up: roll_up_deal demotes
     every uncorroborated reranker claim to `inconclusive` (SIM-253), so letting a
     single web agreement trigger it would silently strip unrelated deck facts from
     the Market/Company tabs on web-agreement-only deals. The caller therefore
     persists these verdicts WITHOUT the roll-up (they change no status).

So the only verdict is confirmation:
    - AGREE (agrees=True): the closest comparable web figure is within ~2x.
    - NO-SIGNAL (nothing emitted): everything else -- a divergence (of ANY size),
      a currency/metric we cannot match, or no web figure. Absence is never a
      conflict, and a divergence is never surfaced here.

Surfacing divergences ("the deck's TAM is far outside public estimates") is real
diligence value, but it needs a non-status-flipping soft signal (an `agrees=None`
presence-only event, which also needs record_corroboration_result's `if not agrees`
tightened to `if agrees is False`) -- a tracked follow-up, not this slice.

Scope: CURRENCY sizing only (TAM/SAM/SOM/market size). CAGR (percent) is a
follow-up. Comparisons run only against the deck's own document claims:
kind=="web" claims (this pass's prior-run collect output) are excluded so the web
never corroborates itself.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from uuid import UUID

from app.models.claim import Claim

# _sizing_label is the single sizing-metric detector (raw-label + value_type
# gated). Imported here so the deck side and the web side canonicalize a metric
# IDENTICALLY -- no second copy to drift. Promoting it to a shared market_sizing
# module is the tracked market/company shared-helper follow-up.
from app.services.corroboration import WEB_SEARCH_SOURCE, CorroborationVerdict
from app.services.market_view import _sizing_label
from app.services.web_search_collect import WebFactCandidate

logger = logging.getLogger(__name__)

OUTSIDE_SOURCE = WEB_SEARCH_SOURCE

# Confirm when the closest comparable web figure is within [1/2, 2]x of the deck
# figure. Anything outside is no-signal -- never a conflict (see module docstring).
_AGREE_MAX_RATIO = 2.0

# Currency-symbol / code -> canonical ISO code, so a "$"-vs-"USD" spelling does
# not block a real comparison while a USD-vs-EUR mismatch still does. An unknown
# token maps to its own uppercased self (compared literally); an absent unit is
# unknown and never matched (returns None -> the pair is skipped).
_CURRENCY_CODE: dict[str, str] = {
    "$": "USD",
    "US$": "USD",
    "USD": "USD",
    "€": "EUR",
    "EUR": "EUR",
    "£": "GBP",
    "GBP": "GBP",
    "C$": "CAD",
    "CAD": "CAD",
    "A$": "AUD",
    "AUD": "AUD",
}

# Symbols that name more than one currency: "¥" is BOTH the Japanese yen and the
# Chinese yuan (CNY), a ~20x FX gap. Treated as unmatchable (code None) so a
# bare-"¥" figure never compares -- against another "¥" it could otherwise falsely
# confirm a CNY figure with a JPY source (or vice versa) whenever their raw
# numbers happen to land within the confirm band. An unambiguous ISO code
# (JPY / CNY / RMB) is required to compare those; it flows through the literal
# branch and matches only its own code.
# Both the ASCII yen sign (U+00A5) and the fullwidth variant (U+FFE5) are ambiguous.
_AMBIGUOUS_CURRENCY_SYMBOLS = frozenset({"¥", "￥"})


def _currency_code(unit: object) -> str | None:
    """Canonical currency code for a unit string, or None when it is absent,
    unusable, or an ambiguous symbol. Conservative: an unrecognized token is
    compared literally (its uppercased self), never coerced to another currency."""
    if not isinstance(unit, str):
        return None
    token = unit.strip()
    if not token or token in _AMBIGUOUS_CURRENCY_SYMBOLS:
        return None
    return _CURRENCY_CODE.get(token, _CURRENCY_CODE.get(token.upper(), token.upper()))


def _currency_amount(claim_value: object) -> tuple[float, str] | None:
    """(positive normalized amount, currency code) for a CURRENCY value, or None
    when the value is not a usable positive currency figure. Zero/negative are
    dropped: a ratio needs a positive denominator, and a non-positive market size
    is an extraction artifact, not a figure to corroborate."""
    if not isinstance(claim_value, dict):
        return None
    if claim_value.get("value_type") != "currency":
        return None
    normalized = claim_value.get("normalized")
    if not isinstance(normalized, (int, float)) or isinstance(normalized, bool):
        return None
    amount = float(normalized)
    if amount <= 0:
        return None
    code = _currency_code(claim_value.get("unit"))
    if code is None:
        return None
    return amount, code


def _candidate_metric(candidate: WebFactCandidate) -> str | None:
    """The sizing-metric key a web candidate names, via the SAME _sizing_label the
    deck side uses (a throwaway Claim carries the candidate's label + value so the
    detector's raw-label + value_type gates apply unchanged)."""
    if candidate.claim_kind != "quantitative":
        return None
    probe = Claim(
        entity=candidate.entity,
        attribute=candidate.attribute,
        attribute_raw=candidate.attribute_raw,
        value=candidate.value,
    )
    label = _sizing_label(probe)
    return label[0] if label is not None else None


def corroborate_sizing_against_web(
    deck_claims: Sequence[Claim],
    web_candidates: Sequence[WebFactCandidate],
) -> list[tuple[UUID, str, CorroborationVerdict]]:
    """Confirm the deck's currency market-sizing claims against the gathered web
    figures. Pure: no DB, no network. Returns (claim_id, source_name, verdict)
    tuples in the shape gather_corroboration produces.

    Every returned verdict has agrees=True: this pass confirms, never conflicts
    (see module docstring). Confirms within ~2x; currency must match; kind=="web"
    deck claims are skipped (no self-corroboration); currency value_type only."""
    # Index comparable web figures by (metric key, currency code).
    web_by_key: dict[tuple[str, str], list[tuple[float, WebFactCandidate]]] = {}
    for candidate in web_candidates:
        metric = _candidate_metric(candidate)
        if metric is None:
            continue
        amount = _currency_amount(candidate.value)
        if amount is None:
            continue
        value, code = amount
        web_by_key.setdefault((metric, code), []).append((value, candidate))

    if not web_by_key:
        return []

    verdicts: list[tuple[UUID, str, CorroborationVerdict]] = []
    for claim in deck_claims:
        if claim.id is None:
            continue
        # Never corroborate a web-collected claim against the web (circular), and
        # only currency sizing figures are in scope for this slice.
        if claim.kind == "web":
            continue
        label = _sizing_label(claim)
        if label is None:
            continue
        deck = _currency_amount(claim.value)
        if deck is None:
            continue
        deck_value, deck_code = deck
        comparable = web_by_key.get((label[0], deck_code))
        if not comparable:
            continue

        # The CLOSEST web figure (smallest multiplicative distance) represents the
        # comparison -- the best public match to the deck, so a far-off outlier is
        # simply ignored rather than blocking a real confirmation.
        web_value, candidate = min(comparable, key=lambda wv: abs(math.log(wv[0] / deck_value)))
        ratio = web_value / deck_value

        # Confirm within band; otherwise no-signal. Never a conflict.
        if not (1 / _AGREE_MAX_RATIO <= ratio <= _AGREE_MAX_RATIO):
            continue

        verdicts.append(
            (
                claim.id,
                OUTSIDE_SOURCE,
                CorroborationVerdict(
                    agrees=True,
                    result={
                        "source_url": candidate.source_url,
                        "source_title": candidate.source_title,
                        "metric": label[0],
                        "metric_label": label[1],
                        "deck_value": deck_value,
                        "web_value": web_value,
                        "web_value_raw": candidate.value.get("raw"),
                        "currency": deck_code,
                        "ratio": round(ratio, 3),
                        "web_figures_considered": len(comparable),
                    },
                ),
            )
        )

    return verdicts
