"""Deal-terms view -- the Cap Table tab's "Key Deal Terms" claims-driven surface.

Same claims-first principle as market_view/company_view/screening_materials: the
claims spine is the ground truth, nothing is invented, and a surface with no
backing claims comes back empty so the tab renders "information not available".

WHY THIS RECOVERS FROM THE CATCH-ALL BUCKETS
============================================
Deal-structure figures -- valuation, investment amount, ownership %, price per
share, share counts, option pool, liquidation preference -- ARE extracted when a
CIM/term sheet states them, but they are NOT financial-statement line items, so
the parser's attribute canonicalizer (Simpero_Gov_AI_Services emit.CoreAttribute)
has no canonical name for them: they land in the OPERATING_METRIC / CORE_UNMAPPED
catch-all buckets, keyed by the document's own raw label. So they are recovered
here the same way market_view recovers TAM/SAM/SOM and company_view recovers
headcount/founded -- by matching a claim's raw label against a closed set of
deal-term phrases, gated by the value_type the slot expects. No parser change and
no re-analysis: this reads claims that already exist.

WHAT IS DELIBERATELY NOT HERE
=============================
- Per-holder capitalization rows (shareholder x shares/ownership/investment). A
  cap table is a table, and claims_from_table (parser extract.py) stamps ONE
  caller-supplied entity on every cell -- "a table does not know whose numbers it
  holds" -- so the shareholder identity is inside attribute_raw, not entity.
  Reconstructing per-holder rows from that would be fragile label-string
  splitting, which misattributes; it needs a dedicated per-shareholder parser
  extractor emitting structured rows (a re-analysis follow-up), not a curation
  pass here.
- Governance rights as a distinct category. The parser's qualitative taxonomy
  (propose.AssertionClass) has no investment/governance-terms class; such
  sentences scatter across commercial_terms / related_party / plan_or_commitment
  and already surface on the Company tab. Surfacing them here would duplicate that
  or require a new assertion_class -- deliberately not fabricated.

Every figure is scoped to the deal's LEAD business subject (a competitor's stated
valuation never surfaces as the target's), picked latest-actual-first then
most-corroborated, and value-guarded. The helpers borrowed from
screening_materials (_fmt_value/_citation/_source_url formatting, _STATUS_RANK)
and subject_fold are shared with market_view/company_view so the tabs cannot
disagree on the same deal.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.models.claim import Claim
from app.services.entity_resolution.resolved import normalize_name
from app.services.screening_materials import (
    _STATUS_RANK,
    _citation,
    _fmt_value,
    _source_url,
)
from app.services.subject_fold import _DISPLAY_STATUSES, UNMATCHED, fold_subjects, subject_of


@dataclass(frozen=True)
class DealTermFact:
    label: str
    value: str
    citation: str | None
    status: str
    entity: str | None
    source_url: str | None = None


@dataclass(frozen=True)
class DealTermsView:
    terms: list[DealTermFact]


# Deal-structure line items, recovered from a claim's raw label the same way
# market_view recovers sizing. Each entry is (key, display, phrases, value_type):
# a phrase matches only as a contiguous run of WHOLE tokens over normalize_name
# (so "share price" never fires inside "market share price index"), and the
# value_type the slot expects GATES the match -- a percent can never fill the
# dollar valuation slot, a currency can never fill the ownership slot. Ordered by
# how a deal summary reads, and SPECIFIC BEFORE GENERIC: pre/post-money precede
# the bare "valuation" slot, so "pre-money valuation" keys pre_money (the first
# match wins) rather than the generic slot.
_TERM_LABELS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    (
        "investment_amount",
        "Investment Amount",
        (
            "investment amount",
            "amount invested",
            "amount of investment",
            "total investment",
            "capital invested",
            "capital raised",
            "amount raised",
            "round size",
            "deal size",
            "investment size",
            "equity investment",
        ),
        "currency",
    ),
    (
        "pre_money",
        "Pre-Money Valuation",
        ("pre money valuation", "pre money", "premoney valuation", "premoney"),
        "currency",
    ),
    (
        "post_money",
        "Post-Money Valuation",
        ("post money valuation", "post money", "postmoney valuation", "postmoney"),
        "currency",
    ),
    # Generic valuation, after the pre/post slots. No bare "value": it is a weak
    # valuation synonym but a strong false positive ("book value", "fair value",
    # "net asset value" are all currency); require an explicit valuation phrase.
    (
        "valuation",
        "Valuation",
        ("valuation", "enterprise value", "equity value"),
        "currency",
    ),
    (
        "ownership",
        "Ownership Stake",
        (
            "ownership",
            "ownership stake",
            "ownership percentage",
            "ownership interest",
            "equity stake",
            "equity ownership",
            "equity interest",
            "percentage ownership",
            "stake acquired",
            "shareholding",
        ),
        "percent",
    ),
    (
        "price_per_share",
        "Price per Share",
        (
            "price per share",
            "per share price",
            "share price",
            "issue price",
            "subscription price",
            "purchase price per share",
        ),
        "currency",
    ),
    (
        "shares_purchased",
        "Shares Purchased",
        ("shares purchased", "shares acquired", "shares issued", "shares subscribed"),
        "count",
    ),
    (
        "fully_diluted_shares",
        "Fully Diluted Shares",
        (
            "fully diluted shares",
            "fully diluted share count",
            "fully diluted shares outstanding",
            "total fully diluted shares",
        ),
        "count",
    ),
    (
        "option_pool",
        "Option Pool",
        ("option pool", "employee option pool", "stock option pool", "esop"),
        "percent",
    ),
    (
        "liquidation_preference",
        "Liquidation Preference",
        ("liquidation preference", "liquidation pref", "liquidation multiple"),
        "ratio",
    ),
)

_TERM_ORDER = {key: i for i, (key, _d, _p, _vt) in enumerate(_TERM_LABELS)}


def _phrase_in(phrase: str, token_list: list[str]) -> bool:
    """Whether `phrase` (space-separated) appears as a contiguous run of WHOLE
    tokens in `token_list` -- a word-boundary match, so "share price" fires on
    "the share price" but NOT inside "market share price movements"."""
    ptoks = phrase.split()
    n = len(ptoks)
    return any(token_list[i : i + n] == ptoks for i in range(len(token_list) - n + 1))


def _term_label(claim: Claim) -> tuple[str, str] | None:
    """The deal term a numeric claim names, as (key, display), or None. Checks the
    raw label first (the document's own words), then the canonical attribute. The
    slot's expected value_type gates the match, so a percent never keys a dollar
    valuation slot and a dollar never keys the ownership slot; an untyped value
    (None) is let through so a legitimately untyped figure is not dropped."""
    claim_vt = claim.value.get("value_type") if isinstance(claim.value, dict) else None
    for source in (claim.attribute_raw, claim.attribute):
        norm = normalize_name(source or "")
        if not norm:
            continue
        token_list = norm.split()
        for key, display, phrases, expected_vt in _TERM_LABELS:
            if claim_vt is not None and claim_vt != expected_vt:
                continue
            if any(_phrase_in(p, token_list) for p in phrases):
                return key, display
    return None


def _term_rank(claim: Claim) -> tuple[int, int, int, float]:
    """Latest-actual-first, mirroring market_view._sizing_rank: a forecast ranks
    below any historical figure (an unmarked period counts as historical), then a
    later year, then a more-corroborated status, then the SIGNED value so a
    negative extraction error sinks below any positive rather than winning on
    magnitude."""
    is_historical = 0 if claim.period_kind in ("E", "P") else 1
    year = claim.period_year if claim.period_year is not None else -1
    normalized = claim.value.get("normalized") if isinstance(claim.value, dict) else None
    magnitude = (
        float(normalized)
        if isinstance(normalized, (int, float)) and not isinstance(normalized, bool)
        else float("-inf")
    )
    return (is_historical, year, _STATUS_RANK.get(claim.status, 0), magnitude)


def build_deal_terms_view(
    claims: Sequence[Claim],
    *,
    filenames: Mapping[uuid.UUID, str],
    source_urls: Mapping[uuid.UUID, str] | None = None,
    dashboard_structure: dict[str, Any] | None = None,
    company: str | None = None,
) -> DealTermsView:
    """Curate the deal's claims into the Key Deal Terms surface: one best figure
    per deal-term slot, recovered by label. Only trust-earned claims are shown;
    an empty result means the deal states no recognizable deal terms."""
    fold = fold_subjects(claims, dashboard_structure, company)

    # key -> (rank, claim, display). The rank leads with a subject priority so the
    # target's own figure always beats an unmapped one for the same slot.
    best: dict[str, tuple[tuple[int, int, int, int, float], Claim, str]] = {}

    for claim in claims:
        # _DISPLAY_STATUSES (not _TRUSTED): surface conflicted + inconclusive deal
        # terms with their true label rather than dropping them; parity with the
        # endpoint query and the sibling views.
        if claim.status not in _DISPLAY_STATUSES:
            continue
        # Deal terms are numeric scalars; the qualitative tier has no
        # investment-terms class (see the module docstring), so skip it here.
        if claim.claim_kind == "qualitative":
            continue
        if _fmt_value(claim.value) == "—":
            continue

        # A single figure wins per slot, so a competitor's stated valuation must
        # not displace the target's. Deal terms are about the target or the deal
        # itself: keep the lead-subject figure, or an UNMATCHED one (a CIM states
        # "the Company's pre-money valuation ..." or elides the subject entirely --
        # both fold to lead/UNMATCHED), but drop one that resolves to a NAMED
        # non-lead subject (a rival's figure).
        subject = subject_of(fold, claim.entity)
        is_lead = subject == fold.lead and fold.lead != UNMATCHED
        if not (is_lead or subject == UNMATCHED):
            continue

        keyed = _term_label(claim)
        if keyed is None:
            continue
        key, display = keyed
        subject_priority = 1 if is_lead else 0
        rank = (subject_priority, *_term_rank(claim))
        current = best.get(key)
        if current is None or rank > current[0]:
            best[key] = (rank, claim, display)

    terms = [
        DealTermFact(
            label=display,
            value=_fmt_value(claim.value),
            citation=_citation(claim, filenames),
            status=claim.status,
            entity=claim.entity,
            source_url=_source_url(claim, source_urls),
        )
        for _key, (_rank, claim, display) in sorted(
            best.items(), key=lambda item: _TERM_ORDER.get(item[0], 99)
        )
    ]
    return DealTermsView(terms=terms)
