"""SEC EDGAR submissions corroboration source -- non-financial identity/geography.

Corroborates a US filer's HEADQUARTERS and STATE OF INCORPORATION against its
EDGAR submissions record (data.sec.gov/submissions/CIK{cik}.json) -- the
structured, externally-filed facts the numeric companyfacts path (SecEdgarSource)
does not carry. This is the first NON-financial corroborator on the EDGAR side:
it follows the ISED adapter's text-fact shape (a marker vocabulary + string
inclusion compare), not sec_edgar's numeric compare.

Entity resolution reuses SecEdgarSource's CIK resolver (company_tickers.json +
brand aliases + suffix-insensitive title match), which SELF-resolves from the
claim's entity -- it does NOT depend on a resolved_entity row (no pipeline job
writes one, which is why ISED/Trademark stay inert). That is why EDGAR fires.

Posture (mirrors the rest of Epic 12): confirm-on-match, decline (return None)
on any unreadable or absent side, and never raise (the gatherer swallows an
exception as no-signal anyway). HEADQUARTERS is CONFIRM-ONLY -- a company states
an operating HQ that can differ from the registered principal office, and HQs
move, so a non-match is no-signal, never a conflict. STATE OF INCORPORATION is a
hard legal fact, so a real mismatch is surfaced as a conflict.
"""

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.models.claim import Claim
from app.services.corroboration import CorroborationVerdict
from app.services.corroboration_sources.sec_edgar import SecEdgarSource

logger = logging.getLogger(__name__)

_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_USER_AGENT = "Simpero corroboration (engineering@simpero.com)"
_TIMEOUT = httpx.Timeout(20.0)

_FACT_STATE_OF_INCORPORATION = "state_of_incorporation"
_FACT_HQ = "headquarters"

# A claim is a state-of-incorporation fact when its text or label carries one of
# these markers; an HQ fact when it does AND is a geographic_presence assertion
# (so a stray "principal office" in a governance sentence isn't read as HQ).
_INCORP_MARKERS = (
    "state of incorporation",
    "jurisdiction of incorporation",
    "incorporated in",
    "incorporated under the laws",
    "domiciled in",
    "state of domicile",
)
_HQ_MARKERS = (
    "headquarter",  # headquarters / headquartered
    "head office",
    "principal executive office",
    "principal office",
    "principal place of business",
)

# US states + DC: postal code -> name. Both directions are matched, so a claim's
# "Delaware" and EDGAR's "DE" resolve to the same canonical code.
_US_STATES: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}
_STATE_NAME_TO_CODE: dict[str, str] = {name.lower(): code for code, name in _US_STATES.items()}

Fetch = Callable[[str], Awaitable[Any]]


async def _default_fetch(url: str) -> Any:
    async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


def _claim_text(claim: Claim) -> str | None:
    """The deck's own words. A qualitative claim's assertion lives in value.raw
    (normalized is null by contract for these text facts)."""
    value = claim.value or {}
    raw = value.get("raw")
    if isinstance(raw, str) and raw.strip():
        return raw
    return None


def _labels(claim: Claim) -> str:
    return " ".join(str(v).lower() for v in (claim.attribute, claim.attribute_raw) if v)


def _fact_for(claim: Claim, text: str) -> str | None:
    """Which submissions fact this claim is about, or None. State of incorporation
    wins over HQ (a sentence may mention both, but incorporation is the harder,
    conflict-eligible fact and should be checked as such)."""
    hay = f"{text.lower()} {_labels(claim)}"
    if any(marker in hay for marker in _INCORP_MARKERS):
        return _FACT_STATE_OF_INCORPORATION
    if claim.assertion_class == "geographic_presence" and any(m in hay for m in _HQ_MARKERS):
        return _FACT_HQ
    return None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _sole_state_in(text: str) -> str | None:
    """The single US state named (by full name) in `text`, as a canonical postal
    code -- or None when none or more than one is named. Ambiguous -> decline,
    never guess. Only full names are matched: a claim's prose spells the state
    out ("Delaware"), and a bare two-letter token is too collision-prone to trust
    as a state."""
    low = _norm(text)
    found = {
        code
        for name, code in _STATE_NAME_TO_CODE.items()
        if re.search(rf"\b{re.escape(name)}\b", low)
    }
    return next(iter(found)) if len(found) == 1 else None


def _result(claim: Claim, cik: int, fact: str, registry_value: str) -> dict[str, Any]:
    return {
        "source": SecEdgarSubmissionsSource.name,
        "cik": cik,
        "fact": fact,
        "claim_value": _claim_text(claim),
        "registry_value": registry_value,
    }


def _check_incorporation(
    claim: Claim, text: str, data: Any, cik: int
) -> CorroborationVerdict | None:
    edgar_code = (data or {}).get("stateOfIncorporation")
    if not isinstance(edgar_code, str) or edgar_code.upper() not in _US_STATES:
        logger.info(
            "EDGAR-submissions no-signal reason=no_state_of_incorporation deal=%s cik=%s",
            claim.deal_id,
            cik,
        )
        return None
    edgar_code = edgar_code.upper()
    claimed = _sole_state_in(text)
    if claimed is None:
        logger.info(
            "EDGAR-submissions no-signal reason=no_state_in_claim deal=%s cik=%s",
            claim.deal_id,
            cik,
        )
        return None
    agrees = claimed == edgar_code
    logger.info(
        "EDGAR-submissions %s deal=%s fact=state_of_incorporation claim=%s edgar=%s cik=%s",
        "verified" if agrees else "CONFLICT",
        claim.deal_id,
        claimed,
        edgar_code,
        cik,
    )
    registry_value = f"{_US_STATES[edgar_code]} ({edgar_code})"
    return CorroborationVerdict(
        agrees=agrees, result=_result(claim, cik, _FACT_STATE_OF_INCORPORATION, registry_value)
    )


def _check_hq(claim: Claim, text: str, data: Any, cik: int) -> CorroborationVerdict | None:
    business = ((data or {}).get("addresses") or {}).get("business") or {}
    city = business.get("city")
    if not isinstance(city, str) or not city.strip():
        logger.info(
            "EDGAR-submissions no-signal reason=no_business_address deal=%s cik=%s",
            claim.deal_id,
            cik,
        )
        return None
    low = _norm(text)
    city_ok = _norm(city) in low
    state = business.get("stateOrCountry")
    state_ok = True
    if isinstance(state, str) and state.upper() in _US_STATES:
        name = _US_STATES[state.upper()].lower()
        state_ok = name in low or re.search(rf"\b{re.escape(state.upper())}\b", text) is not None
    if not (city_ok and state_ok):
        # HQ is confirm-only: a stated operating HQ can differ from the registered
        # principal office, and HQs move, so a non-match is no-signal not a conflict.
        logger.info(
            "EDGAR-submissions no-signal reason=hq_no_match (confirm-only) deal=%s cik=%s",
            claim.deal_id,
            cik,
        )
        return None
    parts = [city.title()]
    if isinstance(state, str) and state.strip():
        parts.append(state.upper())
    registry_value = ", ".join(parts)
    logger.info(
        "EDGAR-submissions verified deal=%s fact=headquarters edgar=%r cik=%s",
        claim.deal_id,
        registry_value,
        cik,
    )
    return CorroborationVerdict(agrees=True, result=_result(claim, cik, _FACT_HQ, registry_value))


class SecEdgarSubmissionsSource:
    """CorroborationSource for SEC EDGAR submissions (HQ + state of incorporation).
    Inject `fetch` in tests; the default hits data.sec.gov with the required
    User-Agent. The one fetch is shared with the composed CIK resolver, so a test
    injects a single fetch for both the tickers lookup and the submissions fetch."""

    name = "sec_edgar_submissions"

    def __init__(self, fetch: Fetch | None = None) -> None:
        self._fetch = fetch or _default_fetch
        # Reuse SecEdgarSource's tested CIK resolver (company_tickers.json + brand
        # aliases + suffix-insensitive title). Composed, not refactored, to leave
        # the financial source untouched; promoting this to a shared resolver
        # (so the ticker map is fetched once, not per SEC source) is a follow-up.
        self._resolver = SecEdgarSource(fetch=self._fetch)

    async def check(self, db: Any, claim: Claim) -> CorroborationVerdict | None:
        text = _claim_text(claim)
        if text is None:
            return None
        fact = _fact_for(claim, text)
        if fact is None:
            return None
        cik = await self._resolver._resolve_cik(claim.entity)
        if cik is None:
            logger.debug(
                "EDGAR-submissions no-signal reason=cik_unresolved deal=%s entity=%r fact=%s",
                claim.deal_id,
                claim.entity,
                fact,
            )
            return None
        try:
            data = await self._fetch(_SUBMISSIONS_URL.format(cik=cik))
        except Exception:
            logger.exception("EDGAR submissions fetch failed for CIK %s; no-signal", cik)
            return None
        if fact == _FACT_STATE_OF_INCORPORATION:
            return _check_incorporation(claim, text, data, cik)
        return _check_hq(claim, text, data, cik)
