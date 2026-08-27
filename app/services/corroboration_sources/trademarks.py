"""Trademark corroboration source -- CIPO, then USPTO (Epic 12, SIM-431).

The Canadian win that a corporate register cannot provide. ISED (SIM-421) can
confirm a company EXISTS; a trademark register can confirm the thing a deck
actually leads with -- that the brand is theirs, registered, and in use since
when they say. Both registers are free and authoritative.

- **CIPO** (Canadian Intellectual Property Office) first, because the target
  book is Canadian.
- **USPTO** second, for a company whose mark is registered in the US instead
  of, or as well as, at home.

**Registered marks only, which is what makes the contract clean.** An
unregistered brand is completely ordinary -- most pre-seed companies have
never filed -- so a mark that is absent, pending, opposed, abandoned or
expired yields None, never a disagreement. Absence is not conflict, and
`conflicted` is sticky.

The two findings this source exists for, both real and both serious:

- **Owner mismatch.** The deck says the brand is theirs; the register says the
  mark is registered to somebody else.
- **First-use mismatch.** The deck says "in market since 2015"; the register's
  declared first use is 2021.

Matching goes through the SIM-420 resolved entity, never a raw name off the
deck. A trademark owner line is a legal name, and "Acme" matching "Acme
Holdings Inc." would either miss the company's own mark or attribute someone
else's to it -- and here that second error produces a `agrees=False` about
brand ownership, which is close to the most damaging false finding this
pipeline could emit.

Deterministic: a closed label vocabulary, explicit mark-text extraction, exact
normalized name matching, and comparisons that decline rather than guess.

NOT registered in app.services.corroboration.CORROBORATION_SOURCES yet: it
goes live only once the corroboration pass's I/O placement is settled
(SIM-416). Same posture as the SEC EDGAR and ISED siblings.

**Endpoint and field-mapping caveat.** The two URL constants below and the
response shapes parsed further down follow each office's published open-data
documentation. The acceptance criteria require hermetic tests and no ticket
authorises live calls from this branch, so neither has been exercised against
a real response here -- re-confirm both before registration. Every parser
declines on an unexpected shape, so the failure mode of a wrong mapping is a
source that says nothing, never one that says something wrong.
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
from app.services.corroboration import CorroborationVerdict
from app.services.entity_resolution.resolved import (
    DealEntity,
    load_resolved_entity,
    normalize_name,
)

logger = logging.getLogger(__name__)

_USER_AGENT = "Simpero corroboration (engineering@simpero.com)"
_TIMEOUT = 10.0

REGISTRY_CIPO = "cipo"
REGISTRY_USPTO = "uspto"

# See the module docstring's endpoint caveat. Kept together, in one block, so
# there is a single place to correct once they are confirmed against live
# responses.
_CIPO_SEARCH_URL = "https://api.ic.gc.ca/opic-cipo/trademarks/v1/marks?q={query}"
_USPTO_SEARCH_URL = "https://developer.uspto.gov/ds-api/trademarks/v1/records?criteria={query}"

Fetch = Callable[[str], Awaitable[Any]]


async def _default_fetch(url: str) -> Any:
    async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


# --------------------------------------------------------------------------
# Which claims this source can speak to.
# --------------------------------------------------------------------------

FACT_TRADEMARK_OWNER = "trademark_owner"
FACT_TRADEMARK_FIRST_USE = "trademark_first_use"

# A closed label vocabulary. A brand claim is qualitative, so `attribute` is
# the deck's own label and there is nothing else to key on. Guessing that an
# unfamiliar label meant "trademark" would put a brand-ownership verdict behind
# a coin flip.
_OWNER_LABELS = frozenset(
    {
        "trademark",
        "trade mark",
        "trademarks",
        "registered trademark",
        "trademark owner",
        "mark owner",
        "brand",
        "brand name",
        "brand ownership",
        "trademark registration",
        "trademark status",
    }
)
_FIRST_USE_LABELS = frozenset(
    {
        "first use",
        "date of first use",
        "first use in commerce",
        "brand first use",
        "trademark first use",
        "in use since",
        "brand in market since",
    }
)


def _fact_for(claim: Claim) -> str | None:
    """Which comparison this claim asks for, or None when it asks for neither.

    First use is checked before ownership: "trademark first use" is in both
    vocabularies by intent, and a claim about a DATE should be compared against
    a date rather than against an owner line.
    """
    labels = {normalize_name(v) for v in (claim.attribute, claim.attribute_raw) if v}
    if labels & _FIRST_USE_LABELS:
        return FACT_TRADEMARK_FIRST_USE
    if labels & _OWNER_LABELS:
        return FACT_TRADEMARK_OWNER
    return None


# --------------------------------------------------------------------------
# What mark the claim is about.
# --------------------------------------------------------------------------

# A quoted mark, straight or curly quotes.
_QUOTED = re.compile(r"[\"“']([^\"”']{1,64})[\"”']")
# A word carrying a registration or trademark symbol: ACME(R), ACME(TM).
_SYMBOLED = re.compile(r"([\w&\-]{2,64})\s*[®™]")
_YEAR = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")


def _claim_text(claim: Claim) -> str | None:
    """The deck's own words. A brand claim is qualitative, so the assertion is
    in `value.raw`."""
    raw = (claim.value or {}).get("raw")
    return raw if isinstance(raw, str) and raw.strip() else None


def _mark_text(text: str, entity: DealEntity) -> str | None:
    """The mark the claim is about.

    Preference order is deliberate. An explicitly marked-up brand -- quoted, or
    carrying (R)/(TM) -- is the deck naming its mark, and that is far better
    evidence than any inference. Only when the deck names no mark at all does
    this fall back to the company's canonical name, which is the common
    pre-seed case (the company and the brand are the same word).

    A mark that normalizes to nothing (punctuation only) is no mark.
    """
    for pattern in (_SYMBOLED, _QUOTED):
        found = {m.strip() for m in pattern.findall(text) if normalize_name(m)}
        if len(found) == 1:
            return next(iter(found))
        if len(found) > 1:
            # Two marks named, one claim: there is no single thing to check,
            # and picking one would report a verdict about the other.
            return None
    return entity.canonical_name if normalize_name(entity.canonical_name) else None


# --------------------------------------------------------------------------
# The register record both offices reduce to.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Mark:
    registry: str
    registration_id: str
    mark_text: str
    owner: str | None
    first_use_year: int | None


# Only a mark that is actually ON the register counts. Pending, opposed,
# abandoned and expired all mean "no registered mark", which is the ordinary
# state of affairs for a pre-seed brand and must read as no-signal.
_REGISTERED_STATUSES = frozenset({"registered", "registration", "registered mark"})


def _str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _rows(payload: Any, *keys: str) -> list[Mapping[str, Any]]:
    """The result rows of a search response, under whichever of `keys` the
    office uses."""
    if not isinstance(payload, Mapping):
        return []
    for key in keys:
        rows = payload.get(key)
        if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
            return [r for r in rows if isinstance(r, Mapping)]
    return []


def _is_registered(status: str | None) -> bool:
    return bool(status) and normalize_name(status or "") in _REGISTERED_STATUSES


def _first_owner(row: Mapping[str, Any], *keys: str) -> str | None:
    """The owner named on the record. A mark can list several owners over its
    life; the first entry is the current one in both offices' published
    ordering."""
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for entry in value:
                if isinstance(entry, Mapping):
                    name = _str(entry.get("name")) or _str(entry.get("partyName"))
                    if name:
                        return name
                elif isinstance(entry, str) and entry.strip():
                    return entry.strip()
    return None


def _year(value: Any) -> int | None:
    if isinstance(value, int) and 1800 <= value <= 2100:
        return value
    if not isinstance(value, str):
        return None
    years = {int(m) for m in _YEAR.findall(value)}
    return next(iter(years)) if len(years) == 1 else None


def _parse_cipo(row: Mapping[str, Any]) -> _Mark | None:
    registration_id = _str(row.get("registrationNumber")) or _str(row.get("applicationNumber"))
    mark_text = _str(row.get("markText")) or _str(row.get("trademarkName"))
    if not registration_id or not mark_text:
        return None
    if not _is_registered(_str(row.get("status")) or _str(row.get("statusDescription"))):
        return None
    return _Mark(
        registry=REGISTRY_CIPO,
        registration_id=registration_id,
        mark_text=mark_text,
        owner=_first_owner(row, "owners", "applicantName", "currentOwner"),
        first_use_year=_year(row.get("dateOfFirstUseInCanada") or row.get("dateOfFirstUse")),
    )


def _parse_uspto(row: Mapping[str, Any]) -> _Mark | None:
    registration_id = _str(row.get("registrationNumber")) or _str(row.get("serialNumber"))
    mark_text = _str(row.get("markIdentification")) or _str(row.get("markText"))
    if not registration_id or not mark_text:
        return None
    if not _is_registered(_str(row.get("status")) or _str(row.get("markCurrentStatus"))):
        return None
    return _Mark(
        registry=REGISTRY_USPTO,
        registration_id=registration_id,
        mark_text=mark_text,
        owner=_first_owner(row, "owners", "ownerName", "partyName"),
        first_use_year=_year(row.get("firstUseDate") or row.get("dateOfFirstUse")),
    )


class TrademarkSource:
    """CorroborationSource for CIPO with a USPTO fallthrough. Inject `fetch` in
    tests; the default hits both offices' public JSON endpoints."""

    name = "trademarks_cipo_uspto"

    def __init__(self, fetch: Fetch | None = None) -> None:
        self._fetch = fetch or _default_fetch

    async def _get(self, url: str) -> Any:
        """A fetch that never raises: an office being unreachable is
        never-checked, and must not stop the other one from answering."""
        try:
            return await self._fetch(url)
        except Exception:
            logger.exception("%s: fetch failed for %s; treating as no-signal", self.name, url)
            return None

    async def _find(self, mark_text: str) -> _Mark | None:
        """The one registered mark with this text, CIPO before USPTO.

        Several distinct registrations of the same text is no-signal: it is
        genuinely ambiguous which one the deck means, and two different owners
        would otherwise make the verdict depend on result order.
        """
        query = quote(mark_text)
        for url, parse, keys in (
            (_CIPO_SEARCH_URL.format(query=query), _parse_cipo, ("marks", "results")),
            (_USPTO_SEARCH_URL.format(query=query), _parse_uspto, ("results", "response", "docs")),
        ):
            found: dict[str, _Mark] = {}
            for row in _rows(await self._get(url), *keys):
                mark = parse(row)
                if mark is not None and normalize_name(mark.mark_text) == normalize_name(mark_text):
                    found[mark.registration_id] = mark
            if len(found) == 1:
                return next(iter(found.values()))
            if len(found) > 1:
                return None
        return None

    async def check(self, db: Any, claim: Claim) -> CorroborationVerdict | None:
        fact = _fact_for(claim)
        if fact is None:
            return None  # not a brand or first-use claim

        entity = await load_resolved_entity(db, claim.deal_id)
        if entity is None:
            return None  # nothing resolved this deal -- no owner to compare against

        # A deck names other companies' brands too (a partner's platform, a
        # competitor's product). Comparing one of those against THIS deal's
        # ownership would be a brand-ownership finding about the wrong company.
        if entity.matches(claim.entity) is None:
            return None

        text = _claim_text(claim)
        if text is None:
            return None

        mark_text = _mark_text(text, entity)
        if mark_text is None:
            return None

        mark = await self._find(mark_text)
        if mark is None:
            # No registered mark. Completely ordinary for a pre-seed brand --
            # absence is never a conflict.
            return None

        return _verdict(fact, text, mark, entity)


def _verdict(fact: str, text: str, mark: _Mark, entity: DealEntity) -> CorroborationVerdict | None:
    if fact == FACT_TRADEMARK_FIRST_USE:
        claimed_year = _year(text)
        if claimed_year is None or mark.first_use_year is None:
            return None
        agrees = claimed_year == mark.first_use_year
        claimed: Any = claimed_year
        registered: Any = mark.first_use_year
    else:
        if mark.owner is None:
            return None
        # The register's owner line is a legal name, which is exactly what the
        # resolved entity carries -- canonical plus every former name, so a
        # mark still registered under the pre-rename name still reads as
        # theirs.
        agrees = entity.matches(mark.owner) is not None
        claimed = entity.canonical_name
        registered = mark.owner

    return CorroborationVerdict(
        agrees=agrees,
        result={
            "source": mark.registry,
            "registry": mark.registry,
            "registration_id": mark.registration_id,
            "mark_text": mark.mark_text,
            "fact": fact,
            "claim_text": text,
            "claim_value": claimed,
            "registry_value": registered,
            "registered_owner": mark.owner,
            "canonical_name": entity.canonical_name,
        },
    )
