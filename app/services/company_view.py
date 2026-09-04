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
permanent empties.

Every fact is scoped to the deal's LEAD business subject (a competitor's or
segment's figure never surfaces as the target's), picked latest-actual-first,
and value-guarded, mirroring screening_materials' rules. The helpers borrowed
from screening_materials -- _fmt_value/_citation formatting, _STATUS_RANK, and
the _TRUSTED status filter (the trust invariant, not just display) -- are
underscore-prefixed internals; promoting them to a shared public module is a
tracked follow-up. The subject fold is kept local so this module does not depend
on that one's evolving internals.
"""

import re
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.models.claim import Claim
from app.services.entity_resolution.resolved import normalize_name
from app.services.screening_materials import _STATUS_RANK, _TRUSTED, _citation, _fmt_value

# The status shown for a sector/HQ fact that came from the deal-profile
# classifier rather than a cited claim -- honest about its (weaker) provenance.
_DERIVED = "derived"

# Sentinel subject for a claim whose entity matched no dashboard subject. A
# distinct string no dashboard/parser emits as a subject name (a NUL byte), so an
# unmatched entity never collides with a real lead subject literally named "Other"
# -- which would otherwise pass the subject filter and surface a rival's facts.
_UNMATCHED = "\x00unmatched"


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

# Company identity metrics recovered from a claim's raw label. `tokens` are whole
# normalized tokens whose presence (in order, as a contiguous run) identifies the
# metric -- not a raw substring, so "employees" matches "Total Employees" but the
# value_type guard (below) is what rejects "Total Employees Turnover %". Each
# metric also names the value types it accepts.
_IDENTITY_LABELS: tuple[tuple[str, str, tuple[tuple[str, ...], ...]], ...] = (
    (
        "headcount",
        "Headcount",
        (
            ("headcount",),
            ("employees",),
            ("fte",),
            ("ftes",),
            ("full", "time", "employees"),
            ("number", "of", "employees"),
            ("total", "employees"),
            ("total", "staff"),
        ),
    ),
    (
        "founded",
        "Founded",
        (
            ("founded",),
            ("year", "founded"),
            ("incorporated",),
            ("date", "of", "incorporation"),
            ("year", "of", "incorporation"),
            ("inception",),
            ("established",),
        ),
    ),
)

_IDENTITY_ORDER = {key: i for i, (key, _d, _t) in enumerate(_IDENTITY_LABELS)}

# Tokens that disqualify a label from an identity metric even when its words match
# and its value type fits: a headcount is a stock ("Total Employees"), never a
# flow or change ("Employees Terminated", "Employee Turnover") -- both are counts,
# so the value-type guard alone cannot tell them apart.
_IDENTITY_EXCLUDE: dict[str, frozenset[str]] = {
    "headcount": frozenset(
        {
            "terminated",
            "turnover",
            "attrition",
            "hired",
            "hires",
            "added",
            "resigned",
            "departures",
            "left",
            "reduction",
            "layoffs",
            "laid",
            "growth",
            "per",
        }
    ),
}

_YEAR_RE = re.compile(r"\b(1[89]\d\d|20\d\d)\b")


def _tokens_contain(phrase: tuple[str, ...], tokens: list[str]) -> bool:
    """True when `phrase` appears as a contiguous run within `tokens`."""
    n = len(phrase)
    return any(tuple(tokens[i : i + n]) == phrase for i in range(len(tokens) - n + 1))


def _identity_label(claim: Claim) -> tuple[str, str] | None:
    for source in (claim.attribute_raw, claim.attribute):
        norm = normalize_name(source or "")
        if not norm:
            continue
        tokens = norm.split()
        token_set = set(tokens)
        for key, display, phrase_sets in _IDENTITY_LABELS:
            if _IDENTITY_EXCLUDE.get(key, frozenset()) & token_set:
                continue
            if any(_tokens_contain(p, tokens) for p in phrase_sets):
                return key, display
    return None


def _identity_value_ok(key: str, claim: Claim) -> bool:
    """A value-type / shape guard so a label's WORDS alone can't mislabel an
    unrelated figure: headcount must be a count (not a "... turnover %"), and a
    founding date must actually look like a plausible year (not a "score")."""
    value = claim.value if isinstance(claim.value, dict) else {}
    if key == "headcount":
        return value.get("value_type") == "count"
    if key == "founded":
        for cand in (value.get("raw"), value.get("normalized")):
            if cand is None:
                continue
            text = (
                f"{int(cand)}"
                if isinstance(cand, (int, float)) and not isinstance(cand, bool)
                else str(cand)
            )
            if _YEAR_RE.search(text):
                return True
        return False
    return True


def _founded_year(claim: Claim) -> int | None:
    """The 4-digit founding year from a claim's value, or None."""
    value = claim.value if isinstance(claim.value, dict) else {}
    for cand in (value.get("normalized"), value.get("raw")):
        if cand is None:
            continue
        text = (
            f"{int(cand)}"
            if isinstance(cand, (int, float)) and not isinstance(cand, bool)
            else str(cand)
        )
        match = _YEAR_RE.search(text)
        if match:
            return int(match.group(0))
    return None


def _identity_value(key: str, claim: Claim) -> str:
    """Display value for an identity fact. A founding year reads "1998", never the
    numeric formatter's comma-grouped "1,998" (value_type "date" falls through to
    _fmt_num otherwise)."""
    if key == "founded":
        year = _founded_year(claim)
        if year is not None:
            return str(year)
    return _fmt_value(claim.value)


def _rank(claim: Claim) -> tuple[int, int, int]:
    """Latest-actual-first: a forecast ranks below any historical figure (an
    unmarked period counts as historical), then a later year, then a more
    corroborated status. Recency, NOT magnitude -- so a company that shrank shows
    its latest headcount, not the larger stale one."""
    is_historical = 0 if claim.period_kind in ("E", "P") else 1
    year = claim.period_year if claim.period_year is not None else -1
    return (is_historical, year, _STATUS_RANK.get(claim.status, 0))


def _fold_subjects(
    claims: Sequence[Claim],
    dashboard_structure: dict[str, Any] | None,
    company: str | None = None,
) -> tuple[str, dict[str, str]]:
    """(lead_subject, {casefolded entity: subject}). The parser's grounded
    organizing pass leads when present; otherwise the deal's own company leads
    whenever its name is known, and only when it is not do the most-mentioned
    entities elect the lead -- so a competitor never wins the lead and surfaces its
    facts as the target's. Keyed casefolded so "American Casino" and "american
    casino" fold to one subject."""
    entity_subject: dict[str, str] = {}
    order: list[str] = []
    subjects = (dashboard_structure or {}).get("subjects")
    if isinstance(subjects, list):
        for subject in subjects:
            if not isinstance(subject, dict) or not subject.get("name"):
                continue
            name = str(subject["name"])
            registered_any = False
            for entity in subject.get("entities") or []:
                if entity and entity.casefold() not in entity_subject:
                    entity_subject[entity.casefold()] = name
                    registered_any = True
            # A name-only subject (empty or all-duplicate entities) can scope no
            # claim. If it still became the lead, EVERY entity-tagged claim would
            # fold to _UNMATCHED and be dropped -- rendering the whole Business
            # Overview tab empty for a deal with perfectly good claims -- so it must
            # not enter `order`; the frequency fallback below then takes over.
            if registered_any:
                order.append(name)
    if not order:
        # No grounded subjects. Count the most-mentioned entities, voting only with
        # TRUSTED, QUANTITATIVE claims: an untrusted mention, or a competitor named
        # across qualitative risk/related-party assertions, must not crown a
        # non-target as lead. (Count by the CASEFOLDED entity so case-variant
        # spellings accumulate.)
        quantitative = [
            c for c in claims if c.entity and c.claim_kind != "qualitative" and c.status in _TRUSTED
        ]
        freq = Counter(c.entity.casefold() for c in quantitative)
        display: dict[str, str] = {}
        for claim in quantitative:
            display.setdefault(claim.entity.casefold(), claim.entity)  # type: ignore[union-attr]
        if company:
            # The deal's own company IS the target, so it leads OUTRIGHT whenever we
            # know its name -- whether or not it appears among the trusted
            # quantitative claims. A competitor with more claims must never win the
            # frequency election and surface its facts as the target's; and a target
            # carrying only qualitative disclosures (no trusted quantitative claim)
            # must still lead, or those disclosures fold to _UNMATCHED and are
            # dropped, blanking the Business Overview tab. The frequency election
            # decides the lead ONLY when the deal has no company name (the else
            # branch); an untagged fact (see _subject_of) or one whose entity IS the
            # company is then kept, and any other named entity folds to _UNMATCHED
            # and is dropped.
            entity_subject[company.casefold()] = display.get(company.casefold(), company)
            order.append(entity_subject[company.casefold()])
        else:
            for folded, _count in sorted(
                ((e, f) for e, f in freq.items() if f >= 2), key=lambda item: (-item[1], item[0])
            ):
                entity_subject[folded] = display.get(folded, folded)
                order.append(entity_subject[folded])
    lead = order[0] if order else _UNMATCHED
    return lead, entity_subject


def _subject_of(entity_subject: dict[str, str], entity: str | None, lead: str) -> str:
    """The subject a claim folds to. An untagged claim (no entity) is taken to be
    about the deal's primary subject (the lead) -- a CIM fact left without an
    entity is about the target by default; a competitor's fact carries its name."""
    if not entity:
        return lead
    return entity_subject.get(entity.casefold(), _UNMATCHED)


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
    dashboard_structure: dict[str, Any] | None = None,
    sector: str | None = None,
    hq_geography: str | None = None,
    company: str | None = None,
) -> CompanyView:
    """Curate the deal's claims (plus the deal-profile sector/HQ) into the
    Business Overview tab. Only trust-earned claims of the lead business subject
    are shown; a section with none comes back empty."""
    lead_subject, entity_subject = _fold_subjects(claims, dashboard_structure, company)

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
        # A related-party assertion's `entity` is the party the relationship names
        # -- for the disclosures that matter (a director, an affiliate, a connected
        # company) that's the THIRD PARTY, not the target, so the plain lead-subject
        # filter would drop exactly the rows this section exists for. Keep such a
        # claim when its entity is unmapped (_UNMATCHED), but still drop one that
        # resolves to a NAMED competitor subject (that fact belongs to the rival).
        is_related_party = (
            claim.claim_kind == "qualitative" and claim.assertion_class == "related_party"
        )
        subject = _subject_of(entity_subject, claim.entity, lead_subject)
        if subject != lead_subject and not (is_related_party and subject == _UNMATCHED):
            continue
        if _fmt_value(claim.value) == "—":
            continue

        if claim.claim_kind == "qualitative":
            section = _SECTION_BY_CLASS.get(claim.assertion_class or "")
            if section is not None:
                sections[section].append(_qual_fact(claim, filenames))
            continue

        keyed = _identity_label(claim)
        if keyed is None:
            continue
        key, display = keyed
        if not _identity_value_ok(key, claim):
            continue
        current = identity_best.get(key)
        if current is None or _rank(claim) > _rank(current[0]):
            identity_best[key] = (claim, display)

    facts.extend(
        CompanyFact(
            label=display,
            value=_identity_value(key, claim),
            citation=_citation(claim, filenames),
            status=claim.status,
            entity=claim.entity,
        )
        for key, (claim, display) in sorted(
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
