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
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.models.claim import Claim
from app.services.entity_resolution.resolved import normalize_name
from app.services.screening_materials import _STATUS_RANK, _TRUSTED, _citation, _fmt_value


def _fold_subjects(
    claims: Sequence[Claim],
    dashboard_structure: dict[str, Any] | None,
    company: str | None,
) -> tuple[str, dict[str, str]]:
    """(lead_subject, {casefolded entity: subject}). The parser's grounded
    organizing pass leads when present; otherwise the most-mentioned entities are
    their own subjects, and -- when none crosses the threshold -- the deal's own
    company anchors the lead. Mirrors company_view/screening_materials, kept local
    (the shared-helper consolidation is a tracked follow-up)."""
    entity_subject: dict[str, str] = {}
    order: list[str] = []
    subjects = (dashboard_structure or {}).get("subjects")
    if isinstance(subjects, list):
        for subject in subjects:
            if not isinstance(subject, dict) or not subject.get("name"):
                continue
            name = str(subject["name"])
            order.append(name)
            for entity in subject.get("entities") or []:
                if entity and entity.casefold() not in entity_subject:
                    entity_subject[entity.casefold()] = name
    # Fall back whenever the dashboard yielded no usable subject -- absent, an
    # empty list, OR a non-empty list whose every entry was malformed (not a dict /
    # no name). Otherwise a malformed structure would leave lead="Other" and
    # silently pass every claim (including a competitor's) through the sizing filter.
    if not order:
        # Elect the most-mentioned entities, then the deal's company. QUALITATIVE
        # claims are excluded from the election: a competitive_position claim's
        # entity is a COMPETITOR and a market_definition's is "the market", so
        # counting them could crown a non-target as the lead subject and let its
        # sizing figures win the slot.
        quantitative = [c for c in claims if c.entity and c.claim_kind != "qualitative"]
        freq = Counter(c.entity.casefold() for c in quantitative)
        display: dict[str, str] = {}
        for claim in quantitative:
            display.setdefault(claim.entity.casefold(), claim.entity)  # type: ignore[union-attr]
        for folded, _count in sorted(
            ((e, f) for e, f in freq.items() if f >= 2), key=lambda item: (-item[1], item[0])
        ):
            entity_subject[folded] = display.get(folded, folded)
            order.append(entity_subject[folded])
        if not order and company:
            entity_subject[company.casefold()] = company
            order.append(company)
    lead = order[0] if order else "Other"
    return lead, entity_subject


def _subject_of(entity_subject: dict[str, str], entity: str | None, lead: str) -> str:
    """The subject a claim folds to. An untagged claim (no entity) is taken to be
    about the deal's primary subject (the lead)."""
    if not entity:
        return lead
    return entity_subject.get(entity.casefold(), "Other")


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
# let it overwrite the real dollar TAM). Only a known currency/percent MISMATCH is
# excluded (see _sizing_label); an untyped or otherwise-typed value still matches.
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

# value_types that are mutually exclusive for a sizing slot: a dollar figure is
# never a percent and vice versa. A claim typed one of these that mismatches a
# label's expected type is skipped; any other type (or none) is allowed through.
_TYPED_INCOMPATIBLE = frozenset({"currency", "percent"})

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
        upper_acronyms = {
            m.group(1).casefold()
            for m in re.finditer(r"\b([A-Za-z]{2,})\b(?![’'`][sS]\b)", source)
            if m.group(1).isupper()
        }
        for key, display, acronyms, phrases, expected_vt in _SIZING_LABELS:
            if claim_vt in _TYPED_INCOMPATIBLE and claim_vt != expected_vt:
                continue
            if (acronyms & upper_acronyms) or any(_phrase_in(p, token_list) for p in phrases):
                return key, display
    return None


def _sizing_rank(claim: Claim) -> tuple[int, int, int, float]:
    # Recency-first, mirroring screening_materials._rank_key: a forecast ranks
    # below any historical figure (an unmarked period counts as historical), then
    # a later year, then a more corroborated status, then the larger magnitude
    # (absolute -- so a market size is never picked by sign). This keeps a stale
    # larger TAM from beating a more current one.
    is_historical = 0 if claim.period_kind in ("E", "P") else 1
    year = claim.period_year if claim.period_year is not None else -1
    normalized = claim.value.get("normalized") if isinstance(claim.value, dict) else None
    magnitude = (
        abs(normalized)
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


def _qual_fact(claim: Claim, filenames: Mapping[uuid.UUID, str]) -> MarketFact:
    """A qualitative assertion as a MarketFact: the entity it is about as the
    label, the assertion text (value.raw, via _fmt_value) as the value, plus its
    citation and trust status. Falls back to a class-appropriate label when the
    claim carries no entity, so the row is never headed by a blank."""
    label = (claim.entity or "").strip() or _QUAL_LABEL_FALLBACK.get(
        claim.assertion_class or "", "—"
    )
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
    lead_subject, entity_subject = _fold_subjects(claims, dashboard_structure, company)

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
                definition.append(_qual_fact(claim, filenames))
            elif claim.assertion_class == "competitive_position":
                competition.append(_qual_fact(claim, filenames))
            continue

        # A single sizing figure wins per key, so a competitor's figure must not
        # displace the target's. Keep an unmapped ("Other") or lead-subject
        # figure; drop one whose entity resolves to a NAMED non-lead subject.
        subject = _subject_of(entity_subject, claim.entity, lead_subject)
        if subject != lead_subject and subject != "Other":
            continue
        keyed = _sizing_label(claim)
        if keyed is None:
            continue
        key, display = keyed
        # Lead-subject priority leads the rank: the target's own figure always
        # beats an "Other" one for the same slot. "Other" covers both a legitimate
        # market-descriptor entity ("the UK market") AND an UNLISTED competitor
        # that folded to "Other" (one not named in dashboard_structure, so the
        # filter above can't drop it) -- without this, that competitor's larger or
        # more recent figure would outrank the target's, since _sizing_rank alone
        # ignores subject. An "Other" figure still fills a slot the lead lacks.
        subject_priority = 1 if subject == lead_subject else 0
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
