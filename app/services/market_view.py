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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.models.claim import Claim
from app.services.entity_resolution.resolved import normalize_name
from app.services.screening_materials import _STATUS_RANK, _TRUSTED, _citation, _fmt_value


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
    ("cagr", "Market Growth (CAGR)", frozenset({"cagr"}), ("market growth rate", "market cagr")),
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


def _sizing_rank(claim: Claim) -> tuple[int, float]:
    # Prefer the more corroborated status, then the larger magnitude -- a stable,
    # deterministic pick when a metric appears several times.
    normalized = claim.value.get("normalized") if isinstance(claim.value, dict) else None
    magnitude = (
        normalized
        if isinstance(normalized, (int, float)) and not isinstance(normalized, bool)
        else float("-inf")
    )
    return (_STATUS_RANK.get(claim.status, 0), magnitude)


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


def build_market_view(claims: Sequence[Claim], *, filenames: Mapping[uuid.UUID, str]) -> MarketView:
    """Curate the deal's claims into the Market tab's view. Only trust-earned
    claims are shown; a section with none comes back empty."""
    sizing_best: dict[str, tuple[Claim, str]] = {}
    definition: list[MarketFact] = []
    competition: list[MarketFact] = []

    for claim in claims:
        if claim.status not in _TRUSTED:
            continue

        if claim.claim_kind == "qualitative":
            if claim.assertion_class == "market_definition":
                definition.append(_qual_fact(claim, filenames))
            elif claim.assertion_class == "competitive_position":
                competition.append(_qual_fact(claim, filenames))
            continue

        if _fmt_value(claim.value) == "—":
            continue
        keyed = _sizing_label(claim)
        if keyed is None:
            continue
        key, display = keyed
        current = sizing_best.get(key)
        if current is None or _sizing_rank(claim) > _sizing_rank(current[0]):
            sizing_best[key] = (claim, display)

    sizing = [
        MarketFact(
            label=display,
            value=_fmt_value(claim.value),
            citation=_citation(claim, filenames),
            status=claim.status,
            entity=claim.entity,
        )
        for _key, (claim, display) in sorted(
            sizing_best.items(), key=lambda item: _SIZING_ORDER.get(item[0], 99)
        )
    ]
    definition.sort(key=_qual_sort)
    competition.sort(key=_qual_sort)
    return MarketView(sizing=sizing, market_definition=definition, competitive_position=competition)
