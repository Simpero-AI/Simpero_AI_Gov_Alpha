"""ISED / Corporations Canada corroboration source, with OrgBook BC fallthrough
(Epic 12, SIM-421).

The highest-value external corroborator for the target book. A Canadian
pre-seed company has no SEC filing and no revenue anyone else has published,
but it IS in a corporate register -- and the register answers exactly the
questions a deck makes claims about: does this company exist, when was it
incorporated, where, is it still active, and where is its office.

Two registers, tried in order, because Canadian coverage is genuinely split:

- **ISED / Corporations Canada** (federal, keyless JSON) -- companies
  incorporated under the CBCA.
- **OrgBook BC** (provincial, keyless JSON) -- many Canadian startups
  incorporate provincially and never appear federally at all. A federal miss
  is the normal case, not an error, so it falls through here rather than
  ending the check.

Both misses return None. Never-checked is not a conflict, and `conflicted` is
sticky: a company neither register knows must come out of this adapter exactly
as it went in.

**Matching is via the SIM-420 resolved entity, never `claim.entity`.**
"Northern Systems" matches a dozen real corporations; picking one and
comparing a deck figure against it manufactures a conflict about a company
nobody was talking about. So the adapter resolves nothing itself: it reads the
deal's canonical name, aliases and pre-resolved registry ids from
`load_resolved_entity`, and accepts a register record only on an unambiguous
match against those. Ambiguous or unmatched is no-signal.

It also checks that the CLAIM is about the deal's own company. A deck names
customers, competitors and investors too, and corroborating "Acme Corp was
incorporated in 1994" -- a claim about a customer -- against the deal
company's registration would be a conflict about the wrong entity entirely.

Deterministic, like the rest of the core: a fixed label vocabulary, exact
normalized name matching, and comparisons that decline rather than guess. No
model output reaches a verdict.

NOT registered in app.services.corroboration.CORROBORATION_SOURCES yet: it
goes live only once the corroboration pass's I/O placement is settled
(SIM-416), so a network call never sits unresolved inside the verify
transaction. Same posture as the SEC EDGAR sibling.

**Field mapping caveat.** The response shapes parsed below follow ISED's and
OrgBook's published open-data schemas. Tests here are hermetic by design (the
acceptance criteria require no live network), so the mapping has not been run
against a live response in this branch -- re-confirm it against real payloads
before registration. The parsers are deliberately tolerant: an unexpected
shape yields None (no-signal) at every step, never a wrong verdict.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from app.models.claim import Claim
from app.models.resolved_entity import (
    REGISTRY_BC_REGISTRATION_NUMBER,
    REGISTRY_ISED_CORPORATION_ID,
)
from app.services.corroboration import CorroborationVerdict
from app.services.entity_resolution.resolved import (
    DealEntity,
    load_resolved_entity,
    normalize_name,
)

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0
_USER_AGENT = "Simpero corroboration (engineering@simpero.com)"

REGISTRY_ISED = "ised"
REGISTRY_ORGBOOK_BC = "orgbook_bc"

# Corporations Canada's open JSON API (keyless).
_ISED_CORPORATION_URL = "https://ised-isde.canada.ca/cc/lgcy/api/corporations/{key}.json"
_ISED_SEARCH_URL = "https://ised-isde.canada.ca/cc/lgcy/api/corporations.json?q={query}"

# OrgBook BC v4 (keyless). Autocomplete finds the topic; the topic carries the
# attributes.
_ORGBOOK_AUTOCOMPLETE_URL = "https://orgbook.gov.bc.ca/api/v4/search/autocomplete?q={query}"
_ORGBOOK_TOPIC_URL = (
    "https://orgbook.gov.bc.ca/api/v4/topic/registration.registries.ca/{registration_id}/formatted"
)

Fetch = Callable[[str], Awaitable[Any]]


async def _default_fetch(url: str) -> Any:
    async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


# --------------------------------------------------------------------------
# Which claims this source can speak to.
# --------------------------------------------------------------------------

# The registry facts a corporate register actually holds. Anything outside
# this set is not something ISED or OrgBook can agree or disagree with.
FACT_INCORPORATION_YEAR = "incorporation_year"
FACT_JURISDICTION = "jurisdiction"
FACT_STATUS = "status"
FACT_HQ_PROVINCE = "hq_province"

# A closed label vocabulary, not a fuzzy classifier. Entity claims are
# qualitative, so `attribute` is the document's own label rather than a value
# from the canonical financial enum -- there is nothing to key on but the
# words the deck used. A label outside this set is no-signal: an adapter that
# guessed which registry fact an unfamiliar label meant would compare the
# wrong two things and call the difference a conflict.
_FACT_LABELS: Mapping[str, frozenset[str]] = {
    FACT_INCORPORATION_YEAR: frozenset(
        {
            "incorporated",
            "incorporation",
            "incorporation date",
            "date of incorporation",
            "year of incorporation",
            "incorporation year",
            "founded",
            "founded in",
            "year founded",
            "founding year",
            "date founded",
            "established",
            "inception",
            "inception date",
        }
    ),
    FACT_JURISDICTION: frozenset(
        {
            "jurisdiction",
            "jurisdiction of incorporation",
            "incorporation jurisdiction",
            "place of incorporation",
            "country of incorporation",
            "province of incorporation",
            "state of incorporation",
            "domicile",
            "incorporated in",
        }
    ),
    FACT_STATUS: frozenset(
        {
            "status",
            "corporate status",
            "company status",
            "legal status",
            "registration status",
            "standing",
            "good standing",
        }
    ),
    FACT_HQ_PROVINCE: frozenset(
        {
            "headquarters",
            "headquarters location",
            "hq",
            "hq location",
            "head office",
            "head office location",
            "registered office",
            "registered address",
            "principal office",
            "principal place of business",
        }
    ),
}


def _fact_for(claim: Claim) -> str | None:
    """The registry fact this claim is about, or None.

    Both `attribute` and `attribute_raw` are consulted: canonicalization maps
    an entity claim's label to one of the catch-alls, so the deck's own words
    survive only in `attribute_raw`.
    """
    labels = {normalize_name(v) for v in (claim.attribute, claim.attribute_raw) if v}
    for fact, vocabulary in _FACT_LABELS.items():
        if labels & vocabulary:
            return fact
    return None


# --------------------------------------------------------------------------
# Jurisdictions and provinces.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Place:
    """A country, and a subdivision within it when one was actually named.
    `subdivision=None` means "no finer than the country", NOT "unknown" -- that
    distinction is what stops a deck saying "Canada" from contradicting a BC
    registration."""

    country: str
    subdivision: str | None = None


_CA_PROVINCES: Mapping[str, str] = {
    "alberta": "AB",
    "ab": "AB",
    "british columbia": "BC",
    "bc": "BC",
    "manitoba": "MB",
    "mb": "MB",
    "new brunswick": "NB",
    "nb": "NB",
    "newfoundland and labrador": "NL",
    "newfoundland": "NL",
    "nl": "NL",
    "northwest territories": "NT",
    "nt": "NT",
    "nova scotia": "NS",
    "ns": "NS",
    "nunavut": "NU",
    "nu": "NU",
    "ontario": "ON",
    "on": "ON",
    "prince edward island": "PE",
    "pe": "PE",
    "quebec": "QC",
    "qc": "QC",
    "saskatchewan": "SK",
    "sk": "SK",
    "yukon": "YT",
    "yt": "YT",
}

# Country-level and non-Canadian markers. Two-letter US state codes are
# deliberately absent: "CA" is California and Canada, "DE" is Delaware and
# Germany, and a wrong expansion here becomes a jurisdiction conflict.
_COUNTRIES: Mapping[str, _Place] = {
    "canada": _Place("CA"),
    "federal": _Place("CA"),
    "canada federal": _Place("CA"),
    "cbca": _Place("CA"),
    "canada business corporations act": _Place("CA"),
    "united states": _Place("US"),
    "united states of america": _Place("US"),
    "usa": _Place("US"),
    "delaware": _Place("US", "DE"),
    "nevada": _Place("US", "NV"),
    "california": _Place("US", "CA"),
    "new york": _Place("US", "NY"),
    "united kingdom": _Place("GB"),
    "england": _Place("GB"),
    "ireland": _Place("IE"),
    "singapore": _Place("SG"),
}

_PLACE_ALIASES: Mapping[str, _Place] = {
    **{alias: _Place("CA", code) for alias, code in _CA_PROVINCES.items()},
    **_COUNTRIES,
}


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(normalize_name(text).split())


# A two-letter province code is recognised only in the register's "City, ON"
# comma form, never as a loose token -- see _places_named_in.
_TRAILING_PROVINCE_CODE = re.compile(r",\s*([A-Za-z]{2})\b")


def _places_named_in(text: str, aliases: Mapping[str, _Place]) -> set[_Place]:
    """Every place `aliases` recognises in `text`.

    Full names match on whole token sequences ("british columbia"), never
    substrings -- a province is nothing inside "toronto". Bare two-letter codes
    are matched ONLY in the register's "City, ON" comma form, never as a loose
    token: "on" standing in prose is the English preposition far more often than
    it is Ontario, and reading it as a province manufactures a location conflict
    out of an ordinary address ("located on Granville Street").
    """
    tokens = _tokens(text)
    found: set[_Place] = set()
    for alias, place in aliases.items():
        if len(alias) == 2 and alias.isalpha():
            continue  # two-letter code: comma form only, handled below
        needle = tuple(alias.split())
        span = len(needle)
        if span and any(tokens[i : i + span] == needle for i in range(len(tokens) - span + 1)):
            found.add(place)
    for match in _TRAILING_PROVINCE_CODE.finditer(text):
        place = aliases.get(match.group(1).casefold())
        if place is not None:
            found.add(place)
    return found


def _sole_place(text: str, aliases: Mapping[str, _Place]) -> _Place | None:
    """The one place `text` names, or None when it names none or several.

    Several is no-signal, not a conflict: "Vancouver office, Delaware holdco"
    is a coherent sentence about two real places, and picking one of them to
    compare against would invent a disagreement.
    """
    found = _places_named_in(text, aliases)
    return next(iter(found)) if len(found) == 1 else None


def _places_agree(claimed: _Place, registered: _Place) -> bool:
    """Whether a deck's stated place is consistent with the register's.

    A coarser claim agrees: a BC-registered company IS in Canada, so a deck
    saying "Canada" is right, not wrong. Only a named subdivision that
    contradicts a named subdivision, or a different country, disagrees.
    """
    if claimed.country != registered.country:
        return False
    if claimed.subdivision is None or registered.subdivision is None:
        return True
    return claimed.subdivision == registered.subdivision


# --------------------------------------------------------------------------
# Corporate status.
# --------------------------------------------------------------------------

STATUS_ACTIVE = "active"
STATUS_DISSOLVED = "dissolved"

_STATUS_WORDS: Mapping[str, str] = {
    "active": STATUS_ACTIVE,
    "in good standing": STATUS_ACTIVE,
    "good standing": STATUS_ACTIVE,
    "operating": STATUS_ACTIVE,
    "registered": STATUS_ACTIVE,
    "dissolved": STATUS_DISSOLVED,
    "struck": STATUS_DISSOLVED,
    "struck off": STATUS_DISSOLVED,
    "cancelled": STATUS_DISSOLVED,
    "revoked": STATUS_DISSOLVED,
    "discontinued": STATUS_DISSOLVED,
    "inactive": STATUS_DISSOLVED,
}

# ISED's published status codes. Only the two unambiguous ones are mapped;
# anything else reads as unknown rather than being forced into "dissolved",
# because "this company is dissolved" is a serious finding and must never be
# manufactured out of a code nobody verified.
_ISED_STATUS_CODES: Mapping[str, str] = {
    "act": STATUS_ACTIVE,
    "active": STATUS_ACTIVE,
    "dis": STATUS_DISSOLVED,
    "dissolved": STATUS_DISSOLVED,
}

# OrgBook's entity_status. "HIS" (historical) is deliberately NOT mapped to
# dissolved -- it marks a superseded credential, which is not the same claim
# about the company, and treating it as dissolution would be a false finding.
_ORGBOOK_STATUS_CODES: Mapping[str, str] = {
    "act": STATUS_ACTIVE,
    "active": STATUS_ACTIVE,
}

# OrgBook entity_type values that mean "incorporated in British Columbia", as
# opposed to an extraprovincial registration of a company incorporated
# elsewhere. Kept deliberately narrow -- an unrecognised type yields no
# jurisdiction rather than a guessed one.
_ORGBOOK_BC_ENTITY_TYPES = frozenset({"bc", "ben", "cc", "ulc"})


def _status_claimed_in(text: str) -> str | None:
    """The corporate status a deck asserts, or None when it asserts none or
    both."""
    tokens = _tokens(text)
    found: set[str] = set()
    for phrase, status in _STATUS_WORDS.items():
        needle = tuple(phrase.split())
        span = len(needle)
        if span and any(tokens[i : i + span] == needle for i in range(len(tokens) - span + 1)):
            found.add(status)
    return next(iter(found)) if len(found) == 1 else None


# --------------------------------------------------------------------------
# The register record both sources reduce to.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _RegistryRecord:
    """One register's answer about one company, in the shape the comparisons
    need. Reducing both registers to this is what lets the federal-then-
    provincial fallthrough be one line instead of two parallel code paths."""

    registry: str
    registry_id: str
    matched_name: str
    incorporation_year: int | None = None
    jurisdiction: _Place | None = None
    status: str | None = None
    office: _Place | None = None
    # Context only, never a verdict input -- see _verdict.
    annual_returns_current: bool | None = None


_YEAR = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")


def _year_in(text: str | None) -> int | None:
    """The single four-digit year in `text`, or None when there are none or
    several. A range ("2019-2021") names two and is therefore no year at
    all."""
    if not text:
        return None
    years = {int(m) for m in _YEAR.findall(text)}
    return next(iter(years)) if len(years) == 1 else None


def _str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _key(name: str) -> str:
    """A JSON attribute key, case-folded. NOT normalize_name -- that is for
    company names and strips the underscores out of an identifier
    ("registration_date" would become "registration date")."""
    return name.strip().casefold()


# --------------------------------------------------------------------------
# ISED parsing.
# --------------------------------------------------------------------------


def _ised_names(payload: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    raw = payload.get("names")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for entry in raw:
            name = _str(entry.get("name")) if isinstance(entry, Mapping) else _str(entry)
            if name:
                names.append(name)
    single = _str(payload.get("name")) or _str(payload.get("legalName"))
    if single:
        names.append(single)
    return names


def _ised_status(payload: Mapping[str, Any]) -> str | None:
    """ISED reports status either as a list of dated entries or as a scalar;
    both shapes appear in its published examples."""
    raw = payload.get("status")
    entry: Any = raw
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        entry = raw[0] if raw else None
    if isinstance(entry, Mapping):
        code = _str(entry.get("code")) or _str(entry.get("label"))
    else:
        code = _str(entry)
    return _ISED_STATUS_CODES.get(_key(code)) if code else None


def _ised_office(payload: Mapping[str, Any]) -> _Place | None:
    office = payload.get("registeredOffice") or payload.get("registeredOfficeAddress")
    if not isinstance(office, Mapping):
        return None
    province = _str(office.get("province")) or _str(office.get("provinceCode"))
    if province:
        code = _CA_PROVINCES.get(normalize_name(province))
        if code:
            return _Place("CA", code)
    # Fall back to the whole address, which must still name exactly one
    # province to count.
    joined = " ".join(str(v) for v in office.values() if isinstance(v, str))
    return _sole_place(joined, {a: p for a, p in _PLACE_ALIASES.items() if p.country == "CA"})


def _ised_annual_returns_current(payload: Mapping[str, Any]) -> bool | None:
    returns = payload.get("annualReturns")
    if not isinstance(returns, Sequence) or isinstance(returns, (str, bytes)) or not returns:
        return None
    latest = returns[0] if isinstance(returns[0], Mapping) else None
    if latest is None:
        return None
    filed = latest.get("filed")
    return bool(filed) if isinstance(filed, bool) else None


def _parse_ised(payload: Any, entity: DealEntity) -> _RegistryRecord | None:
    """An ISED corporation record, or None when it is not this company or not
    parseable. The name check is re-run even when the record was fetched by id:
    a stale or mistyped id must surface as no-signal, not as a comparison
    against someone else's corporation."""
    if not isinstance(payload, Mapping):
        return None
    corporation_id = _str(payload.get("corporationId")) or _str(payload.get("corporationNumber"))
    if not corporation_id:
        return None

    matched = next((m for m in (entity.matches(n) for n in _ised_names(payload)) if m), None)
    if matched is None:
        return None

    return _RegistryRecord(
        registry=REGISTRY_ISED,
        registry_id=corporation_id,
        matched_name=matched,
        incorporation_year=_year_in(_str(payload.get("dateOfIncorporation"))),
        # A record in Corporations Canada IS a federal incorporation.
        jurisdiction=_Place("CA"),
        status=_ised_status(payload),
        office=_ised_office(payload),
        annual_returns_current=_ised_annual_returns_current(payload),
    )


def _ised_search_ids(payload: Any, entity: DealEntity) -> str | None:
    """The one corporationId in a search response whose name is this company's,
    or None when none or several are.

    Distinct ids, not row count: a register legitimately returns one
    corporation under several of its own names, and counting rows would read
    that single company as an ambiguous match and discard a real answer.
    """
    results = (
        (payload.get("results") or payload.get("corporations"))
        if isinstance(payload, Mapping)
        else payload
    )
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
        return None

    ids: set[str] = set()
    for row in results:
        if not isinstance(row, Mapping):
            continue
        corporation_id = _str(row.get("corporationId")) or _str(row.get("corporationNumber"))
        if not corporation_id:
            continue
        if any(entity.matches(name) for name in _ised_names(row)):
            ids.add(corporation_id)
    return next(iter(ids)) if len(ids) == 1 else None


# --------------------------------------------------------------------------
# OrgBook BC parsing.
# --------------------------------------------------------------------------


def _orgbook_attributes(topic: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    raw = topic.get("attributes")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for entry in raw:
            if isinstance(entry, Mapping):
                key, value = _str(entry.get("type")), _str(entry.get("value"))
                if key and value:
                    out[_key(key)] = value
    elif isinstance(raw, Mapping):
        for key, value in raw.items():
            if isinstance(value, str) and value.strip():
                out[_key(key)] = value.strip()
    return out


def _orgbook_names(topic: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    raw = topic.get("names")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for entry in raw:
            if isinstance(entry, Mapping):
                name = _str(entry.get("text")) or _str(entry.get("value"))
            else:
                name = _str(entry)
            if name:
                names.append(name)
    return names


def _parse_orgbook(topic: Any, entity: DealEntity) -> _RegistryRecord | None:
    if not isinstance(topic, Mapping):
        return None
    registration_id = _str(topic.get("source_id")) or _str(topic.get("sourceId"))
    if not registration_id:
        return None

    matched = next((m for m in (entity.matches(n) for n in _orgbook_names(topic)) if m), None)
    if matched is None:
        return None

    attributes = _orgbook_attributes(topic)
    status_code = attributes.get("entity_status")
    entity_type = _key(attributes.get("entity_type") or "")
    return _RegistryRecord(
        registry=REGISTRY_ORGBOOK_BC,
        registry_id=registration_id,
        matched_name=matched,
        incorporation_year=_year_in(attributes.get("registration_date")),
        # Only a BC-INCORPORATED entity licenses a "this company is a BC
        # company" verdict. A BC registry entry can equally be an
        # extraprovincial registration of an Alberta or federal corporation,
        # and reading that as a BC incorporation would contradict a perfectly
        # accurate deck.
        jurisdiction=_Place("CA", "BC") if entity_type in _ORGBOOK_BC_ENTITY_TYPES else None,
        status=_ORGBOOK_STATUS_CODES.get(_key(status_code)) if status_code else None,
        # Deliberately no office: a BC registration says nothing reliable about
        # where the company's registered office actually is, and guessing would
        # turn an ordinary head office elsewhere into a location conflict.
        office=None,
    )


def _orgbook_registration_id(payload: Any, entity: DealEntity) -> str | None:
    """The one BC registration id whose name is this company's. Distinct ids,
    not row count -- autocomplete returns one row per matching NAME, so a
    company with a former name legitimately appears twice."""
    results = payload.get("results") if isinstance(payload, Mapping) else payload
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
        return None

    ids: set[str] = set()
    for row in results:
        if not isinstance(row, Mapping):
            continue
        source_id = _str(row.get("topic_source_id")) or _str(row.get("source_id"))
        name = _str(row.get("value")) or _str(row.get("name"))
        if source_id and name and entity.matches(name):
            ids.add(source_id)
    return next(iter(ids)) if len(ids) == 1 else None


# --------------------------------------------------------------------------
# The source.
# --------------------------------------------------------------------------


def _claim_text(claim: Claim) -> str | None:
    """The deck's own words for this claim. Entity claims are qualitative --
    `normalized` is null by contract and the assertion lives in `raw` -- so raw
    is what a register can be compared against."""
    value = claim.value or {}
    raw = value.get("raw")
    if isinstance(raw, str) and raw.strip():
        return raw
    normalized = value.get("normalized")
    if isinstance(normalized, (int, float)) and not isinstance(normalized, bool):
        return str(normalized)
    return None


class IsedCorporationsSource:
    """CorroborationSource for Corporations Canada, falling through to OrgBook
    BC. Inject `fetch` in tests; the default hits the two public JSON APIs."""

    name = "ised_corporations_canada"

    def __init__(self, fetch: Fetch | None = None) -> None:
        self._fetch = fetch or _default_fetch

    async def _get(self, url: str) -> Any:
        """A fetch that never raises. A register being unreachable is
        never-checked, which is not a conflict -- and it must not stop the
        other register from answering."""
        try:
            return await self._fetch(url)
        except Exception:
            logger.exception("%s: fetch failed for %s; treating as no-signal", self.name, url)
            return None

    async def _federal(self, entity: DealEntity) -> _RegistryRecord | None:
        corporation_id = entity.registry_id(REGISTRY_ISED_CORPORATION_ID)
        if corporation_id:
            return _parse_ised(
                await self._get(_ISED_CORPORATION_URL.format(key=quote(corporation_id))), entity
            )

        for name in entity.names:
            found = _ised_search_ids(
                await self._get(_ISED_SEARCH_URL.format(query=quote(name))), entity
            )
            if found is None:
                continue  # no match, or several corporations -- try the next name
            record = _parse_ised(
                await self._get(_ISED_CORPORATION_URL.format(key=quote(found))), entity
            )
            if record is not None:
                return record
        return None

    async def _provincial(self, entity: DealEntity) -> _RegistryRecord | None:
        registration_id = entity.registry_id(REGISTRY_BC_REGISTRATION_NUMBER)
        if registration_id:
            return _parse_orgbook(
                await self._get(_ORGBOOK_TOPIC_URL.format(registration_id=quote(registration_id))),
                entity,
            )

        for name in entity.names:
            found = _orgbook_registration_id(
                await self._get(_ORGBOOK_AUTOCOMPLETE_URL.format(query=quote(name))), entity
            )
            if found is None:
                continue
            record = _parse_orgbook(
                await self._get(_ORGBOOK_TOPIC_URL.format(registration_id=quote(found))), entity
            )
            if record is not None:
                return record
        return None

    async def check(self, db: Any, claim: Claim) -> CorroborationVerdict | None:
        fact = _fact_for(claim)
        if fact is None:
            return None  # not a claim a corporate register holds an answer to

        entity = await load_resolved_entity(db, claim.deal_id)
        if entity is None:
            return None  # nothing resolved this deal -- no company to look up

        # The claim must be about the DEAL's company. A deck names customers,
        # competitors and investors too, and checking their incorporation
        # against this deal's registration would be a conflict about the wrong
        # entity.
        if entity.matches(claim.entity) is None:
            return None

        record = await self._federal(entity)
        if record is None:
            # The normal case, not an error: most Canadian startups incorporate
            # provincially and never appear federally at all.
            record = await self._provincial(entity)
        if record is None:
            return None  # neither register knows this company -- never-checked

        return _verdict(fact, claim, record, entity)


# Within incorporation-year and HQ-province, only a subset of labels licenses a
# CONFLICT on mismatch. The rest confirm on a match but decline on a difference,
# because the difference is ordinarily true rather than contradictory -- and
# `conflicted` is sticky, so manufacturing one from an ordinary fact is the exact
# failure this adapter exists to avoid.
#
# Founding is not incorporation: a company commonly operates a year or two before
# it incorporates, so "founded 2015, incorporated 2019" is a true statement. A
# year claim is a hard incorporation assertion only when neither its label nor
# its wording invokes founding.
_FOUNDING_LABELS: frozenset[str] = frozenset(
    {
        "founded",
        "founded in",
        "year founded",
        "founding year",
        "date founded",
        "established",
        "inception",
        "inception date",
    }
)
_FOUNDING_WORDS: frozenset[str] = frozenset(
    {"founded", "founding", "establish", "established", "inception", "since"}
)

# A registered office is a legal filing address (often a law firm's); an operating
# headquarters is where the company actually is. They legitimately sit in
# different provinces, so only the registered-office labels hard-compare against
# the register's registered office -- an operating-HQ label confirms on a match
# but declines on a mismatch rather than asserting a location conflict.
_REGISTERED_OFFICE_LABELS: frozenset[str] = frozenset({"registered office", "registered address"})


def _is_founding_year_claim(claim: Claim) -> bool:
    """Whether this year claim is about FOUNDING rather than incorporation -- by
    its label or by the wording of its value. A founding year that predates
    incorporation is ordinary, so its mismatch is no-signal, never a conflict."""
    labels = {normalize_name(v) for v in (claim.attribute, claim.attribute_raw) if v}
    if labels & _FOUNDING_LABELS:
        return True
    text = _claim_text(claim)
    return bool(text and set(normalize_name(text).split()) & _FOUNDING_WORDS)


def _is_registered_office_claim(claim: Claim) -> bool:
    """Whether this HQ claim is about the legal REGISTERED OFFICE -- which can
    hard-compare against the register -- rather than an operating headquarters,
    which cannot: a Canadian company with an out-of-province office is ordinary."""
    labels = {normalize_name(v) for v in (claim.attribute, claim.attribute_raw) if v}
    return bool(labels & _REGISTERED_OFFICE_LABELS)


def _verdict(
    fact: str, claim: Claim, record: _RegistryRecord, entity: DealEntity
) -> CorroborationVerdict | None:
    """Compare the one fact the claim asserts against the register's answer.

    Returns None whenever either side is unreadable. A comparison that cannot
    be made is no-signal -- the alternative, treating "we could not parse the
    deck's wording" as disagreement, would make every unusual phrasing a
    conflict.
    """
    text = _claim_text(claim)
    if text is None:
        return None

    claimed: Any
    registered: Any

    if fact == FACT_INCORPORATION_YEAR:
        claimed = _year_in(text)
        registered = record.incorporation_year
        if claimed is None or registered is None:
            return None
        agrees = claimed == registered
        # A founding year that differs from the incorporation year is ordinary,
        # not a contradiction -- decline rather than manufacture a conflict.
        if not agrees and _is_founding_year_claim(claim):
            return None
    elif fact == FACT_JURISDICTION:
        claimed = _sole_place(text, _PLACE_ALIASES)
        registered = record.jurisdiction
        if claimed is None or registered is None:
            return None
        agrees = _places_agree(claimed, registered)
    elif fact == FACT_STATUS:
        claimed = _status_claimed_in(text)
        registered = record.status
        if claimed is None or registered is None:
            return None
        agrees = claimed == registered
    elif fact == FACT_HQ_PROVINCE:
        # Canadian provinces only. A registered office is not necessarily the
        # operating headquarters, and a Canadian company with a US office is
        # ordinary -- so a deck naming no Canadian province is no-signal here
        # rather than a location conflict.
        claimed = _sole_place(text, {a: p for a, p in _PLACE_ALIASES.items() if p.country == "CA"})
        registered = record.office
        if claimed is None or registered is None:
            return None
        agrees = _places_agree(claimed, registered)
        # Only the legal registered office hard-compares. An operating-HQ label
        # in a different province from the registered office is ordinary (the
        # registered office is often a law firm's), so decline rather than
        # conflict when the label is not a registered-office label.
        if not agrees and not _is_registered_office_claim(claim):
            return None
    else:  # pragma: no cover - _fact_for cannot produce anything else
        return None

    return CorroborationVerdict(
        agrees=agrees,
        result={
            "source": REGISTRY_ISED if record.registry == REGISTRY_ISED else REGISTRY_ORGBOOK_BC,
            "registry": record.registry,
            "registry_id": record.registry_id,
            "matched_name": record.matched_name,
            "canonical_name": entity.canonical_name,
            "fact": fact,
            "claim_value": text,
            "registry_value": _describe(registered),
            # Context, never a verdict input: a late annual return is a fact
            # about filing housekeeping, not about anything the deck claimed,
            # and letting it drive `agrees` would mark a deal conflicted over
            # something nobody asserted.
            "annual_returns_current": record.annual_returns_current,
        },
    )


def _describe(value: Any) -> Any:
    if isinstance(value, _Place):
        return value.subdivision or value.country
    return value


__all__ = [
    "FACT_HQ_PROVINCE",
    "FACT_INCORPORATION_YEAR",
    "FACT_JURISDICTION",
    "FACT_STATUS",
    "IsedCorporationsSource",
    "REGISTRY_ISED",
    "REGISTRY_ORGBOOK_BC",
]
