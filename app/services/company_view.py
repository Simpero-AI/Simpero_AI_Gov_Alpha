"""Company view -- the Business Overview tab's claims-driven surface.

Same claims-first principle as market_view/screening_materials: the claims spine
is the ground truth, nothing is invented, and a section with no backing claims
comes back empty so the tab renders "information not available".

What maps to what:
- facts: the company's identity -- sector and HQ from the deal profile (the
  parser's deal_profile classifier, carried on the deal row), plus headcount and
  founded recovered from a claim's raw label.
- overview: `operating_model` assertions (what the business is, how it operates
  and makes money -- covers the "Business Overview / Business Model / Technology
  & Operations" ground, which the extraction taxonomy does not split further).
- risks: `risk_or_dependency` assertions.
- commercial: `commercial_terms` assertions (customers, pricing, contract terms).
- related_parties: `related_party` assertions.
- plans: `plan_or_commitment` assertions.

market_definition / competitive_position live on the Market tab, not here.
Sections the pipeline has no source for (funding history, co-investor syndicate,
per-region breakdown) are deliberately not surfaced rather than shown as
permanent empties. Formatting/citation/trust helpers are shared with
screening_materials so every surface agrees.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.models.claim import Claim
from app.services.entity_resolution.resolved import normalize_name
from app.services.screening_materials import _STATUS_RANK, _TRUSTED, _citation, _fmt_value

# The status shown for a sector/HQ fact that came from the deal-profile
# classifier rather than a cited claim -- honest about its (weaker) provenance.
_DERIVED = "derived"


@dataclass(frozen=True)
class CompanyFact:
    label: str
    value: str
    citation: str | None
    status: str
    entity: str | None


@dataclass(frozen=True)
class CompanyView:
    facts: list[CompanyFact] = field(default_factory=list)
    overview: list[CompanyFact] = field(default_factory=list)
    risks: list[CompanyFact] = field(default_factory=list)
    commercial: list[CompanyFact] = field(default_factory=list)
    related_parties: list[CompanyFact] = field(default_factory=list)
    plans: list[CompanyFact] = field(default_factory=list)


# Qualitative assertion_class -> the CompanyView section it feeds. Market classes
# are intentionally absent (they belong to the Market tab).
_SECTION_BY_CLASS = {
    "operating_model": "overview",
    "risk_or_dependency": "risks",
    "commercial_terms": "commercial",
    "related_party": "related_parties",
    "plan_or_commitment": "plans",
}

# Company identity metrics recovered from a claim's raw label (matched over
# normalize_name). Acronyms match only as a standalone token; phrases as a
# substring -- same discipline as market_view's sizing labels.
_IDENTITY_LABELS: tuple[tuple[str, str, frozenset[str], tuple[str, ...]], ...] = (
    (
        "headcount",
        "Headcount",
        frozenset({"fte", "ftes"}),
        (
            "headcount",
            "employees",
            "full time employees",
            "number of employees",
            "total employees",
            "total staff",
        ),
    ),
    (
        "founded",
        "Founded",
        frozenset(),
        (
            "year founded",
            "founded",
            "incorporated",
            "date of incorporation",
            "year of incorporation",
            "inception",
            "established",
        ),
    ),
)

_IDENTITY_ORDER = {key: i for i, (key, _d, _a, _p) in enumerate(_IDENTITY_LABELS)}


def _identity_label(claim: Claim) -> tuple[str, str] | None:
    for source in (claim.attribute_raw, claim.attribute):
        norm = normalize_name(source or "")
        if not norm:
            continue
        tokens = set(norm.split())
        for key, display, acronyms, phrases in _IDENTITY_LABELS:
            if (acronyms & tokens) or any(phrase in norm for phrase in phrases):
                return key, display
    return None


def _identity_rank(claim: Claim) -> tuple[int, float]:
    normalized = claim.value.get("normalized") if isinstance(claim.value, dict) else None
    magnitude = (
        normalized
        if isinstance(normalized, (int, float)) and not isinstance(normalized, bool)
        else float("-inf")
    )
    return (_STATUS_RANK.get(claim.status, 0), magnitude)


def _qual_fact(claim: Claim, filenames: Mapping[uuid.UUID, str]) -> CompanyFact:
    return CompanyFact(
        label=claim.entity or "",
        value=_fmt_value(claim.value),
        citation=_citation(claim, filenames),
        status=claim.status,
        entity=claim.entity,
    )


def _qual_sort(fact: CompanyFact) -> tuple[int, str]:
    return (-_STATUS_RANK.get(fact.status, 0), fact.value.lower())


def build_company_view(
    claims: Sequence[Claim],
    *,
    filenames: Mapping[uuid.UUID, str],
    sector: str | None = None,
    hq_geography: str | None = None,
    company: str | None = None,
) -> CompanyView:
    """Curate the deal's claims (plus the deal-profile sector/HQ) into the
    Business Overview tab. Only trust-earned claims are shown; a section with none
    comes back empty."""
    facts: list[CompanyFact] = []
    if sector:
        facts.append(CompanyFact("Sector", sector, None, _DERIVED, company))
    if hq_geography:
        facts.append(CompanyFact("Headquarters", hq_geography, None, _DERIVED, company))

    identity_best: dict[str, tuple[Claim, str]] = {}
    sections: dict[str, list[CompanyFact]] = {name: [] for name in set(_SECTION_BY_CLASS.values())}

    for claim in claims:
        if claim.status not in _TRUSTED:
            continue

        if claim.claim_kind == "qualitative":
            section = _SECTION_BY_CLASS.get(claim.assertion_class or "")
            if section is not None:
                sections[section].append(_qual_fact(claim, filenames))
            continue

        if _fmt_value(claim.value) == "—":
            continue
        keyed = _identity_label(claim)
        if keyed is None:
            continue
        key, display = keyed
        current = identity_best.get(key)
        if current is None or _identity_rank(claim) > _identity_rank(current[0]):
            identity_best[key] = (claim, display)

    facts.extend(
        CompanyFact(
            label=display,
            value=_fmt_value(claim.value),
            citation=_citation(claim, filenames),
            status=claim.status,
            entity=claim.entity,
        )
        for _key, (claim, display) in sorted(
            identity_best.items(), key=lambda item: _IDENTITY_ORDER.get(item[0], 99)
        )
    )
    for section_facts in sections.values():
        section_facts.sort(key=_qual_sort)

    return CompanyView(
        facts=facts,
        overview=sections["overview"],
        risks=sections["risks"],
        commercial=sections["commercial"],
        related_parties=sections["related_parties"],
        plans=sections["plans"],
    )
