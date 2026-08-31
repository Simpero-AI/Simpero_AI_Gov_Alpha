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
    if isinstance(subjects, list) and subjects:
        for subject in subjects:
            if not isinstance(subject, dict) or not subject.get("name"):
                continue
            name = str(subject["name"])
            order.append(name)
            for entity in subject.get("entities") or []:
                if entity and entity.casefold() not in entity_subject:
                    entity_subject[entity.casefold()] = name
    else:
        freq = Counter(c.entity.casefold() for c in claims if c.entity)
        display: dict[str, str] = {}
        for claim in claims:
            if claim.entity:
                display.setdefault(claim.entity.casefold(), claim.entity)
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
# standalone token so "SOM" never fires on "some"/"wholesome"; a full phrase
# matches as a substring. Ordered by how the sizing funnel reads.
_SIZING_LABELS: tuple[tuple[str, str, frozenset[str], tuple[str, ...]], ...] = (
    ("tam", "TAM", frozenset({"tam"}), ("total addressable market",)),
    (
        "sam",
        "SAM",
        frozenset({"sam"}),
        ("serviceable addressable market", "serviceable available market"),
    ),
    ("som", "SOM", frozenset({"som"}), ("serviceable obtainable market",)),
    (
        "market_size",
        "Market Size",
        frozenset(),
        ("market size", "addressable market", "market value", "industry size"),
    ),
    # No bare "cagr" acronym: a standalone "cagr" token fires on "Revenue CAGR"
    # (a company growth rate, not a market one), so require a market-qualified
    # phrase for this row.
    (
        "cagr",
        "Market Growth (CAGR)",
        frozenset(),
        ("market growth rate", "market cagr", "market growth"),
    ),
)

_SIZING_ORDER = {key: i for i, (key, _d, _a, _p) in enumerate(_SIZING_LABELS)}


def _sizing_label(claim: Claim) -> tuple[str, str] | None:
    """The market-size metric a numeric claim names, as (key, display), or None.
    Checks the raw label first (the document's own words), then the canonical
    attribute."""
    for source in (claim.attribute_raw, claim.attribute):
        norm = normalize_name(source or "")
        if not norm:
            continue
        tokens = set(norm.split())
        for key, display, acronyms, phrases in _SIZING_LABELS:
            if (acronyms & tokens) or any(phrase in norm for phrase in phrases):
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


def _qual_fact(claim: Claim, filenames: Mapping[uuid.UUID, str]) -> MarketFact:
    """A qualitative assertion as a MarketFact: the entity it is about as the
    label, the assertion text (value.raw, via _fmt_value) as the value, plus its
    citation and trust status."""
    return MarketFact(
        label=claim.entity or "",
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
    return MarketView(sizing=sizing, market_definition=definition, competitive_position=competition)
