"""Market view -- the Market tab's claims-driven surface.

Like screening_materials, the claims spine is the ground truth. Three things are
curated from it:

- sizing: numeric market-size figures (TAM/SAM/SOM/market size/CAGR), recovered
  from a claim's raw label the same way the screening panel recovers headline
  financials -- most CIMs of an operating business carry none, and that is a
  correct empty, not a gap to paper over.
- market_definition / competitive_position: the qualitative assertions the
  parser's qualitative tier emits (claim_kind='qualitative', categorized by
  assertion_class), each shown verbatim with its citation and trust status.

Nothing is invented: a section with no backing claims comes back empty and the
tab renders "information not available". Formatting, the citation string and the
trust filter are shared with screening_materials so the two surfaces agree.
"""

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.models.claim import Claim
from app.services.entity_resolution.resolved import normalize_name
from app.services.screening_materials import _STATUS_RANK, _TRUSTED, _citation, _fmt_value
from app.services.subject_fold import UNMATCHED, fold_subjects, strip_legal_suffix, subject_of

# An UNMATCHED sizing entity is either a legitimate market descriptor ("the UK
# student housing market") -- whose figure IS the deal's market and may fill an
# empty slot -- or an unlisted competitor ("BigRival") whose figure must not
# masquerade as the deal's own market size. Subject-folding can't separate them, so
# a market/industry/sector token in the entity name is the discriminator. Precision
# over recall: a rare descriptor without one of these words is conservatively
# dropped (the section reads "not available") rather than risk surfacing a rival's
# figure as the deal's.
_MARKET_DESCRIPTOR_TOKENS = frozenset(
    {"market", "markets", "industry", "industries", "sector", "sectors"}
)


def _is_market_descriptor(entity: str | None) -> bool:
    """Whether an UNMATCHED sizing entity reads as a market/industry descriptor
    rather than a company name.

    A named company that merely CONTAINS a market/industry/sector token ("The
    Fresh Market, Inc.", "Sector Alarm AS", "Market Basket", "Industry Dive") is
    NOT a descriptor -- and since a claim's entity text can originate from an
    outside intake upload, precision matters. Require the token as the TRAILING
    head noun ("the UK student housing MARKET") and reject any name carrying a
    legal suffix (which marks a named entity, not a market). Precision over
    recall: a rare descriptor without a trailing market/industry/sector word is
    still dropped rather than risk surfacing a rival's figure as the deal's."""
    if not entity:
        return True
    normalized = normalize_name(entity)
    tokens = normalized.split()
    if not tokens:
        return True
    if strip_legal_suffix(entity) != normalized:
        return False
    return tokens[-1] in _MARKET_DESCRIPTOR_TOKENS


@dataclass(frozen=True)
class MarketFact:
    label: str
    value: str
    citation: str | None
    status: str
    entity: str | None


@dataclass(frozen=True)
class MarketView:
    sizing: list[MarketFact]
    market_definition: list[MarketFact]
    competitive_position: list[MarketFact]


# Market-size line items, recovered from a claim's raw label (matched over
# normalize_name, reused from entity resolution). An acronym matches only as a
# standalone ALL-CAPS token in the original label (so "SOM" never fires on
# "some"/"wholesome" or on a possessive fragment like "Sam's"); a phrase matches
# only as a contiguous run of whole tokens (so "market size" never fires inside
# "Supermarket Size"). Ordered by how the sizing funnel reads.
# Each entry is (key, display, acronyms, phrases, value_type): the value_type a
# claim must carry to key this slot. A market SIZE is a dollar figure and CAGR is
# a percent, so the type gates the match -- a "TAM CAGR" percent can never land in
# the dollar TAM slot (where _sizing_rank, blind to value_type, could otherwise
# let it overwrite the real dollar TAM). Any TYPED value that isn't the slot's
# expected type is excluded (see _sizing_label); only an untyped value matches.
_SIZING_LABELS: tuple[tuple[str, str, frozenset[str], tuple[str, ...], str], ...] = (
    ("tam", "TAM", frozenset({"tam"}), ("total addressable market",), "currency"),
    (
        "sam",
        "SAM",
        frozenset({"sam"}),
        ("serviceable addressable market", "serviceable available market"),
        "currency",
    ),
    ("som", "SOM", frozenset({"som"}), ("serviceable obtainable market",), "currency"),
    (
        "market_size",
        "Market Size",
        frozenset(),
        ("market size", "addressable market", "market value", "industry size"),
        "currency",
    ),
    # No bare "cagr" acronym: a standalone "cagr" token fires on "Revenue CAGR"
    # (a company growth rate, not a market one), so require a market-qualified
    # phrase for this row.
    (
        "cagr",
        "Market Growth (CAGR)",
        frozenset(),
        ("market growth rate", "market cagr", "market growth"),
        "percent",
    ),
)

_SIZING_ORDER = {key: i for i, (key, _d, _a, _p, _vt) in enumerate(_SIZING_LABELS)}

# Cap on each qualitative list (market_definition / competitive_position). A
# claim-dense CIM can surface dozens of competitor/market assertions; the tab
# shows the most-corroborated first (see _qual_sort), so the cap keeps the
# strongest rows and never lets an unbounded list swamp the view. Mirrors the
# bounded screening panels.
_QUAL_LIMIT = 12


def _phrase_in(phrase: str, token_list: list[str]) -> bool:
    """Whether `phrase` (space-separated) appears as a contiguous run of WHOLE
    tokens in `token_list` -- a word-boundary match, so "market size" fires on
    "estimated market size" but NOT inside "supermarket size"."""
    ptoks = phrase.split()
    n = len(ptoks)
    return any(token_list[i : i + n] == ptoks for i in range(len(token_list) - n + 1))


def _sizing_label(claim: Claim) -> tuple[str, str] | None:
    """The market-size metric a numeric claim names, as (key, display), or None.
    Checks the raw label first (the document's own words), then the canonical
    attribute. A label's expected value_type gates the match (see _SIZING_LABELS),
    so a percent never keys a dollar-size slot and a dollar never keys CAGR."""
    claim_vt = claim.value.get("value_type") if isinstance(claim.value, dict) else None
    for source in (claim.attribute_raw, claim.attribute):
        source = source or ""
        norm = normalize_name(source)
        if not norm:
            continue
        token_list = norm.split()
        # Acronyms must be standalone ALL-CAPS tokens in the ORIGINAL label
        # (TAM/SAM/SOM are written uppercase), EXCLUDING a possessive like "SAM'S"
        # (a retail comp's revenue line, not Serviceable Addressable Market): the
        # apostrophe-s marks a possessive noun, not an acronym, in both "Sam's" and
        # all-caps "SAM'S". The negative lookahead drops the token before that 's.
        #
        # But an ALL-CAPS token is only EVIDENCE of an acronym against a mixed-case
        # backdrop. A uniformly-uppercase label (the common Docling table form,
        # "REVENUE | SAM | FY23") carries no case signal, so treating every caps
        # token as an acronym misreads a revenue line as Serviceable Addressable
        # Market. When the label is all-caps, accept an acronym only if the label
        # is a single alpha token that IS the acronym ("TAM"); otherwise the
        # acronyms fall away and only a full phrase can key a slot.
        if any(c.islower() for c in source):
            upper_acronyms = {
                m.group(1).casefold()
                for m in re.finditer(r"\b([A-Za-z]{2,})\b(?![’'`][sS]\b)", source)
                if m.group(1).isupper()
            }
        else:
            alpha_tokens = re.findall(r"\b[A-Za-z]{2,}\b", source)
            upper_acronyms = {alpha_tokens[0].casefold()} if len(alpha_tokens) == 1 else set()
        for key, display, acronyms, phrases, expected_vt in _SIZING_LABELS:
            # A claim's TYPED value gates the slot: any known value_type that is
            # not the slot's expected one is excluded -- a "count"/"ratio"/percent
            # can't be a dollar market size, and a currency can't be CAGR. Only an
            # untyped value (None) is let through, so a legitimately untyped size
            # is not dropped. (A denylist of just currency/percent let "count" and
            # "ratio" claims silently overwrite the real dollar TAM.)
            if claim_vt is not None and claim_vt != expected_vt:
                continue
            if (acronyms & upper_acronyms) or any(_phrase_in(p, token_list) for p in phrases):
                return key, display
    return None


def _sizing_rank(claim: Claim) -> tuple[int, int, int, float]:
    # Recency-first, mirroring screening_materials._rank_key: a forecast ranks
    # below any historical figure (an unmarked period counts as historical), then
    # a later year, then a more corroborated status, then the SIGNED value.
    # Signed, not absolute, for BOTH slot types: a larger positive market size
    # wins, while a negative figure -- an extraction error for a currency size, a
    # legitimately shrinking market for a CAGR -- sinks below any positive rather
    # than winning on magnitude (abs() let a parenthesized -$5B beat a real $2B,
    # and preferred -20% over +5% for CAGR). This keeps a stale larger TAM from
    # beating a more current one.
    is_historical = 0 if claim.period_kind in ("E", "P") else 1
    year = claim.period_year if claim.period_year is not None else -1
    normalized = claim.value.get("normalized") if isinstance(claim.value, dict) else None
    magnitude = (
        float(normalized)
        if isinstance(normalized, (int, float)) and not isinstance(normalized, bool)
        else float("-inf")
    )
    return (is_historical, year, _STATUS_RANK.get(claim.status, 0), magnitude)


# A qualitative fact whose entity is blank still needs a non-empty label
# (MarketFactResponse.label is required, and a blank renders as an empty row
# header): a market_definition assertion is about the market itself, a
# competitive_position one about an unnamed competitor.
_QUAL_LABEL_FALLBACK = {
    "market_definition": "The market",
    "competitive_position": "Competitor",
}


def _qual_fact(
    claim: Claim, filenames: Mapping[uuid.UUID, str], display_names: dict[str, str]
) -> MarketFact:
    """A qualitative assertion as a MarketFact: the entity it is about as the
    label, the assertion text (value.raw, via _fmt_value) as the value, plus its
    citation and trust status. Falls back to a class-appropriate label when the
    claim carries no entity, so the row is never headed by a blank.

    `display_names` folds entity-name variants to one canonical spelling
    (normalize_name -> first-seen), so "Acme Corp.", "ACME" and "Acme
    Corporation" all head the same row rather than reading as three competitors."""
    entity = (claim.entity or "").strip()
    if entity:
        key = strip_legal_suffix(entity) or normalize_name(entity)
        label = display_names.get(key, entity)
    else:
        label = _QUAL_LABEL_FALLBACK.get(claim.assertion_class or "", "—")
    return MarketFact(
        label=label,
        value=_fmt_value(claim.value),
        citation=_citation(claim, filenames),
        status=claim.status,
        entity=claim.entity,
    )


def _qual_sort(fact: MarketFact) -> tuple[int, str]:
    # Most-corroborated first, then alphabetical by text for a stable order.
    return (-_STATUS_RANK.get(fact.status, 0), fact.value.lower())


def build_market_view(
    claims: Sequence[Claim],
    *,
    filenames: Mapping[uuid.UUID, str],
    dashboard_structure: dict[str, Any] | None = None,
    company: str | None = None,
) -> MarketView:
    """Curate the deal's claims into the Market tab's view. Only trust-earned
    claims are shown; a section with none comes back empty."""
    fold = fold_subjects(claims, dashboard_structure, company)

    # Canonical display spelling per qualitative entity (normalize_name ->
    # first-seen), so name variants ("Acme Corp." / "ACME" / "Acme Corporation")
    # head the SAME Competitive Position / Market Definition row instead of
    # reading as separate competitors.
    qual_display: dict[str, str] = {}
    for claim in claims:
        if claim.claim_kind == "qualitative" and claim.entity and claim.status in _TRUSTED:
            # Key on the suffix-stripped core (normalize_name keeps legal suffixes,
            # so "Acme Corp." / "ACME" / "Acme Corporation" would NOT fold under it);
            # fall back to normalize_name for a name that is all-suffix.
            key = strip_legal_suffix(claim.entity) or normalize_name(claim.entity)
            qual_display.setdefault(key, claim.entity.strip())

    # key -> (rank, claim, display). The rank leads with a subject priority so the
    # target's own figure always outranks an "Other" one for the same slot (see
    # the sizing loop); the remaining elements are _sizing_rank's recency/magnitude.
    sizing_best: dict[str, tuple[tuple[int, int, int, int, float], Claim, str]] = {}
    definition: list[MarketFact] = []
    competition: list[MarketFact] = []

    for claim in claims:
        if claim.status not in _TRUSTED:
            continue
        if _fmt_value(claim.value) == "—":
            continue

        if claim.claim_kind == "qualitative":
            # Qualitative market facts are deliberately NOT subject-filtered: a
            # market_definition fact is about the market (its entity is often "the
            # market", not the target) and a competitive_position fact is ABOUT a
            # competitor -- scoping either to the target's lead subject would drop
            # exactly the rows these sections exist to show.
            if claim.assertion_class == "market_definition":
                definition.append(_qual_fact(claim, filenames, qual_display))
            elif claim.assertion_class == "competitive_position":
                competition.append(_qual_fact(claim, filenames, qual_display))
            continue

        # A single sizing figure wins per key, so a competitor's figure must not
        # displace the target's. Keep the lead-subject figure, or an unmapped one
        # ONLY when its entity reads as a market descriptor (a market/industry figure
        # legitimately fills a slot the target lacks); drop one whose entity is a
        # NAMED non-lead subject OR an unlisted competitor (an unmapped bare company
        # name), so a rival's figure never surfaces as the deal's own market size.
        subject = subject_of(fold, claim.entity)
        # A real lead is never the UNMATCHED sentinel. When the deal has no dashboard
        # subject, nothing crosses the frequency threshold, and no company name (e.g.
        # deal.name==""), fold.lead is itself UNMATCHED -- and a named competitor
        # also folds to UNMATCHED, so a bare `subject == fold.lead` would treat it
        # as the lead and let its figure through. Require fold.lead != UNMATCHED so
        # such a claim falls to the market-descriptor gate instead of passing unchecked.
        is_lead = subject == fold.lead and fold.lead != UNMATCHED
        if not (is_lead or (subject == UNMATCHED and _is_market_descriptor(claim.entity))):
            continue
        keyed = _sizing_label(claim)
        if keyed is None:
            continue
        key, display = keyed
        # Lead-subject priority leads the rank: the target's own figure always beats
        # an unmapped market-descriptor figure for the same slot. A descriptor figure
        # still fills a slot the lead lacks (subject_priority 0), but can no longer
        # outrank the target's own.
        subject_priority = 1 if is_lead else 0
        rank = (subject_priority, *_sizing_rank(claim))
        current = sizing_best.get(key)
        if current is None or rank > current[0]:
            sizing_best[key] = (rank, claim, display)

    sizing = [
        MarketFact(
            label=display,
            value=_fmt_value(claim.value),
            citation=_citation(claim, filenames),
            status=claim.status,
            entity=claim.entity,
        )
        for _key, (_rank, claim, display) in sorted(
            sizing_best.items(), key=lambda item: _SIZING_ORDER.get(item[0], 99)
        )
    ]
    definition.sort(key=_qual_sort)
    competition.sort(key=_qual_sort)
    return MarketView(
        sizing=sizing,
        market_definition=definition[:_QUAL_LIMIT],
        competitive_position=competition[:_QUAL_LIMIT],
    )
