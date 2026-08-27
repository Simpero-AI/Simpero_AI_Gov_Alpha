"""US Federal Register corroboration source -- the regulatory-context lane
(Epic 12, SIM-422).

**Honest scope, stated up front.** For the target book -- Canadian pre-seed --
this source has near-zero company-fact value. A company at that stage has no
US federal footprint, so the Federal Register has nothing to say about its
revenue, its headcount, or its incorporation. Wiring it as a company-fact
corroborator would produce a source that returns None on essentially every
claim, and whose rare hit would more likely be a name collision than a
finding.

So it is wired as something narrower and genuinely useful instead: a
**regulatory-context** signal. When a deck asserts that a specific rule,
approval, clearance or enforcement action exists, this checks whether the
Federal Register actually carries an agency document to that effect.

**Presence-only, and deliberately so.** This source can return an agreement or
nothing -- never a disagreement. Absence from the Federal Register is not
evidence that a regulatory fact is false: the rule may be a state rule, a
Canadian or EU one, older than the corpus the search reaches, or simply
described in the deck with words that do not match the document's title.
Turning any of those into `agrees=False` would mark a deal `conflicted` --
which is sticky -- on the strength of a search that missed. Only a genuine
agency presence yields an event.

Two lanes, both requiring the claim to name something specific enough to look
up:

- **Instrument** -- the claim cites a CFR part, a Federal Register citation, or
  an agency docket. The search is for that citation, and a result counts only
  if the document really carries it.
- **Entity footprint** -- the claim names a US federal agency and a company
  distinctive enough to search for. The search is restricted to that agency,
  and a result counts only if the document actually names the company.

A claim that names neither is out of scope. Searching the Federal Register for
"we comply with applicable regulations" returns thousands of notices, none of
which corroborate anything.

Deterministic: closed agency vocabulary, regex citations, exact normalized
name matching. Nothing model-derived reaches a verdict.

NOT registered in app.services.corroboration.CORROBORATION_SOURCES yet: it
goes live only once the corroboration pass's I/O placement is settled
(SIM-416), so a network call never sits unresolved inside the verify
transaction. Same posture as the SEC EDGAR sibling.

Follow-up, not a gap in this ticket: entity-lane matching uses `claim.entity`
because SIM-422 depends on SIM-416 only. Once SIM-420's resolved entity is
available it should key on that instead, which would add the deal's former
names to the search and remove the distinctiveness guard below.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from app.models.claim import Claim
from app.services.corroboration import CorroborationVerdict

logger = logging.getLogger(__name__)

_USER_AGENT = "Simpero corroboration (engineering@simpero.com)"
_TIMEOUT = 10.0
_BASE_URL = "https://www.federalregister.gov/api/v1/documents.json"
_PER_PAGE = 20

# Only claims the pipeline itself typed as regulatory (SIM-364) are in scope.
# Using the contract's own type rather than re-deriving one from the wording is
# what keeps this deterministic -- and a claim nothing typed as regulatory is
# not a regulatory claim just because it mentions an agency in passing.
_REGULATORY = "regulatory"

LANE_INSTRUMENT = "instrument"
LANE_ENTITY = "entity_footprint"

Fetch = Callable[[str], Awaitable[Any]]


async def _default_fetch(url: str) -> Any:
    async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


# --------------------------------------------------------------------------
# Vocabulary.
# --------------------------------------------------------------------------

# Agency name/abbreviation -> the Federal Register's own agency slug, which is
# what `conditions[agencies][]` filters on.
#
# Abbreviations that collide with ordinary English or with citation shorthand
# are deliberately ABSENT: "SEC" is also "sec. 4.2", "DOE" is also a redacted
# party name, "DOT" is also punctuation shorthand. Their full names are here
# instead. A missed agency costs a no-signal; a wrong one costs a corroboration
# against an unrelated agency's docket.
_AGENCY_SLUGS: Mapping[str, str] = {
    "fda": "food-and-drug-administration",
    "food and drug administration": "food-and-drug-administration",
    "fcc": "federal-communications-commission",
    "federal communications commission": "federal-communications-commission",
    "epa": "environmental-protection-agency",
    "environmental protection agency": "environmental-protection-agency",
    "securities and exchange commission": "securities-and-exchange-commission",
    "ftc": "federal-trade-commission",
    "federal trade commission": "federal-trade-commission",
    "usda": "agriculture-department",
    "department of agriculture": "agriculture-department",
    "faa": "federal-aviation-administration",
    "federal aviation administration": "federal-aviation-administration",
    "nhtsa": "national-highway-traffic-safety-administration",
    "osha": "occupational-safety-and-health-administration",
    "cfpb": "consumer-financial-protection-bureau",
    "fincen": "financial-crimes-enforcement-network",
    "cms": "centers-for-medicare-medicaid-services",
    "department of energy": "energy-department",
    "department of transportation": "transportation-department",
    "usfws": "fish-and-wildlife-service",
    "ferc": "federal-energy-regulatory-commission",
    "fmcsa": "federal-motor-carrier-safety-administration",
}

# "21 CFR 820", "21 C.F.R. Part 820".
_CFR = re.compile(r"\b(\d{1,2})\s*C\.?\s*F\.?\s*R\.?\s*(?:part\s*)?(\d{1,4})\b", re.IGNORECASE)
# "89 FR 12345" -- a Federal Register citation.
_FR_CITATION = re.compile(r"\b(\d{2,3})\s*F\.?\s*R\.?\s*(\d{3,6})\b", re.IGNORECASE)
# "FDA-2020-N-1234", "EPA-HQ-OAR-2021-0317".
_DOCKET = re.compile(r"\b[A-Z]{2,6}(?:-[A-Z0-9]{1,6})*-\d{4}-[A-Z0-9]{1,4}-\d{3,5}\b")

_SEPARATORS = re.compile(r"[\W_]+", flags=re.UNICODE)


def _normalize(text: str) -> str:
    """Case-folded, punctuation-stripped, whitespace-collapsed. Same shape as
    the SEC EDGAR sibling's `_normalize`; kept local rather than shared because
    the two adapters are independently replaceable."""
    return " ".join(_SEPARATORS.sub(" ", text.casefold()).split())


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(_normalize(text).split())


def _contains_tokens(haystack: str, needle: Sequence[str]) -> bool:
    """Whole-token containment, not substring: "epa" is the agency standing
    alone and nothing at all inside "therapeutic"."""
    if not needle:
        return False
    hay = _tokens(haystack)
    span = len(needle)
    return any(tuple(hay[i : i + span]) == tuple(needle) for i in range(len(hay) - span + 1))


# --------------------------------------------------------------------------
# What the claim asks about.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Instrument:
    """A citable regulatory instrument named in a claim, and the query that
    finds it."""

    kind: str
    display: str
    query: str


def _claim_text(claim: Claim) -> str:
    """Everything the claim says, in one string.

    A regulatory claim is qualitative, so its assertion lives in `value.raw`
    and -- by the claims contract -- its `attribute` is the document's own
    label rather than a canonical financial name, which is where the agency
    usually appears ("FDA clearance"). `section` carries the deck heading the
    claim sat under, which is often where the citation is printed.
    """
    value = claim.value or {}
    parts = [
        claim.attribute,
        claim.section,
        value.get("raw") if isinstance(value.get("raw"), str) else None,
    ]
    return " ".join(p for p in parts if p)


def _instrument_in(text: str) -> _Instrument | None:
    """The one citable instrument the claim names, or None when it names none
    or several. Several is no-signal: a claim citing two rules gives no single
    thing to confirm, and confirming one of them would overstate what was
    checked."""
    found: list[_Instrument] = []
    for title, part in _CFR.findall(text):
        found.append(
            _Instrument(kind="cfr", display=f"{title} CFR {part}", query=f"{title} CFR {part}")
        )
    for volume, page in _FR_CITATION.findall(text):
        found.append(
            _Instrument(
                kind="fr_citation", display=f"{volume} FR {page}", query=f"{volume} FR {page}"
            )
        )
    for docket in _DOCKET.findall(text):
        found.append(_Instrument(kind="docket", display=docket, query=docket))

    unique = {(i.kind, i.display): i for i in found}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _agency_in(text: str) -> tuple[str, str] | None:
    """The one agency the claim names, as (display, slug), or None when it
    names none or several."""
    found: dict[str, str] = {}
    for name, slug in _AGENCY_SLUGS.items():
        if _contains_tokens(text, name.split()):
            found[slug] = name
    if len(found) != 1:
        return None
    slug, name = next(iter(found.items()))
    return name, slug


def _is_distinctive(entity: str) -> bool:
    """Whether a company name is specific enough to search the Federal Register
    by.

    A single short word ("Block", "Stripe", "Apex") appears in thousands of
    agency notices that have nothing to do with the company, and this source
    has no resolved entity to disambiguate against yet. Two tokens, or one long
    one, is the line: it costs a few real matches and buys back the whole class
    of one-common-word false corroborations.
    """
    tokens = _tokens(entity)
    return len(tokens) >= 2 or (len(tokens) == 1 and len(tokens[0]) >= 8)


# --------------------------------------------------------------------------
# Reading the Federal Register's answer.
# --------------------------------------------------------------------------


def _results(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    rows = payload.get("results")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []
    return [r for r in rows if isinstance(r, Mapping)]


def _document_text(document: Mapping[str, Any]) -> str:
    return " ".join(str(document.get(field) or "") for field in ("title", "abstract", "citation"))


def _document_agency_slugs(document: Mapping[str, Any]) -> set[str]:
    agencies = document.get("agencies")
    if not isinstance(agencies, Sequence) or isinstance(agencies, (str, bytes)):
        return set()
    return {
        str(a.get("slug"))
        for a in agencies
        if isinstance(a, Mapping) and isinstance(a.get("slug"), str)
    }


def _carries_instrument(document: Mapping[str, Any], instrument: _Instrument) -> bool:
    """Whether the document really carries the cited instrument, rather than
    merely coming back from a full-text search for it.

    The API's term search matches the body, so a rule that mentions another
    rule in passing scores too. Confirming a claim on that would be
    corroborating the wrong document.
    """
    if instrument.kind == "cfr":
        title, _, part = instrument.display.partition(" CFR ")
        references = document.get("cfr_references")
        if isinstance(references, Sequence) and not isinstance(references, (str, bytes)):
            for reference in references:
                if not isinstance(reference, Mapping):
                    continue
                if (
                    str(reference.get("title")) == title.strip()
                    and str(reference.get("part")) == part.strip()
                ):
                    return True
        return False

    if instrument.kind == "docket":
        dockets = document.get("docket_ids")
        if isinstance(dockets, Sequence) and not isinstance(dockets, (str, bytes)):
            return any(str(d).strip() == instrument.display for d in dockets)
        return False

    # An FR citation identifies the document itself.
    return _normalize(str(document.get("citation") or "")) == _normalize(instrument.display)


def _search_url(term: str, agency_slug: str | None = None) -> str:
    params: list[tuple[str, str]] = [
        ("conditions[term]", term),
        ("per_page", str(_PER_PAGE)),
        ("order", "relevance"),
    ]
    for field in (
        "document_number",
        "title",
        "abstract",
        "type",
        "citation",
        "docket_ids",
        "cfr_references",
        "agencies",
        "publication_date",
        "html_url",
    ):
        params.append(("fields[]", field))
    if agency_slug:
        params.append(("conditions[agencies][]", agency_slug))
    return f"{_BASE_URL}?{urlencode(params)}"


def _describe(document: Mapping[str, Any]) -> dict:
    return {
        "document_number": document.get("document_number"),
        "title": document.get("title"),
        "type": document.get("type"),
        "citation": document.get("citation"),
        "publication_date": document.get("publication_date"),
        "html_url": document.get("html_url"),
    }


class FederalRegisterSource:
    """CorroborationSource for federalregister.gov. Inject `fetch` in tests;
    the default hits the public keyless API.

    Returns an agreement or None -- never a disagreement. See the module
    docstring for why absence from the Federal Register cannot be read as
    contradiction.
    """

    name = "us_federal_register"

    def __init__(self, fetch: Fetch | None = None) -> None:
        self._fetch = fetch or _default_fetch

    async def _get(self, url: str) -> Any:
        try:
            return await self._fetch(url)
        except Exception:
            logger.exception("%s: fetch failed for %s; treating as no-signal", self.name, url)
            return None

    async def check(self, db: Any, claim: Claim) -> CorroborationVerdict | None:
        if claim.claim_type != _REGULATORY:
            return None  # not a regulatory assertion; nothing to look up

        text = _claim_text(claim)
        if not text.strip():
            return None

        instrument = _instrument_in(text)
        if instrument is not None:
            return await self._confirm_instrument(claim, instrument)

        agency = _agency_in(text)
        if agency is not None:
            return await self._confirm_entity_footprint(claim, agency)

        # A regulatory claim naming neither a citable instrument nor an agency.
        # Searching for it would return noise, and noise is not corroboration.
        return None

    async def _confirm_instrument(
        self, claim: Claim, instrument: _Instrument
    ) -> CorroborationVerdict | None:
        payload = await self._get(_search_url(instrument.query))
        for document in _results(payload):
            if _carries_instrument(document, instrument):
                return CorroborationVerdict(
                    agrees=True,
                    result={
                        "source": self.name,
                        "lane": LANE_INSTRUMENT,
                        "instrument": instrument.display,
                        "instrument_kind": instrument.kind,
                        "claim_text": _claim_text(claim),
                        "document": _describe(document),
                    },
                )
        # The instrument may well exist -- it could predate the search corpus,
        # or be a state or foreign rule. Absence is no-signal, never a conflict.
        return None

    async def _confirm_entity_footprint(
        self, claim: Claim, agency: tuple[str, str]
    ) -> CorroborationVerdict | None:
        agency_name, agency_slug = agency
        if not _is_distinctive(claim.entity):
            return None

        payload = await self._get(_search_url(claim.entity, agency_slug=agency_slug))
        needle = _tokens(claim.entity)
        for document in _results(payload):
            # Both guards, not either: the agency filter can be widened by the
            # API's own agency hierarchy, and a term search matches bodies that
            # merely mention the company's industry.
            if agency_slug not in _document_agency_slugs(document):
                continue
            if not _contains_tokens(_document_text(document), needle):
                continue
            return CorroborationVerdict(
                agrees=True,
                result={
                    "source": self.name,
                    "lane": LANE_ENTITY,
                    "agency": agency_name,
                    "agency_slug": agency_slug,
                    "entity": claim.entity,
                    "claim_text": _claim_text(claim),
                    "document": _describe(document),
                },
            )
        # The common case for the target book: a Canadian pre-seed company has
        # no US federal footprint at all. That is not a finding.
        return None
