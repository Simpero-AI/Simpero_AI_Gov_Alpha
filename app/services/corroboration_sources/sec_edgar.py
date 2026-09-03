"""SEC EDGAR corroboration source (Epic 12) — the first real CorroborationSource.

Given a financial claim, read the deal's SEC CIK from the deal-scoped resolved
entity (SIM-420) and compare the claim's figure to EDGAR's XBRL company facts for
the same period. It agrees within a tight tolerance, disagrees on a material delta
(recording both values + the delta so the conflict view can show them), and
returns no-signal (None) for everything it cannot compare -- no resolved entity,
no SEC CIK resolved for the deal, an attribute it does not map, no reported fact
for that period, or a non-USD unit. Absence is never a conflict (handover
surfacing rule 9.3.5).

Keying on the resolved entity's CIK, NOT on `claim.entity`, is the invariant
`entity_resolution.resolved` documents: a raw deck string ("Acme", "Acme Inc.")
looked up by name in a registry produces the common-name false positive that
would flip a claim to the sticky, unrecoverable `conflicted`. The CIK here is the
one the SEC resolver already resolved deterministically for the deal; when none
is resolved, EDGAR simply has nothing to compare and returns None.

Registered in app.services.corroboration.CORROBORATION_SOURCES. Because it keys on
the resolved CIK, a deal with no SEC-resolved entity is a clean no-signal, never a
name-matched guess.
"""

import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.claim import Claim
from app.models.resolved_entity import REGISTRY_CIK
from app.services.corroboration import CorroborationVerdict
from app.services.entity_resolution.resolved import DealEntity, load_resolved_entity

logger = logging.getLogger(__name__)

# SEC requires a descriptive User-Agent and rate-limits ~10 req/s; the caller
# (the corroboration pass) owns cross-call rate-limiting when this is registered.
_USER_AGENT = "Simpero corroboration (engineering@simpero.com)"
_TIMEOUT = 10.0
# A company's facts change rarely, so they are cached -- but not for the worker's
# whole life: a company that refiles a restated 10-K/A must become visible without
# a process restart. Cache entries refresh after this TTL. Env-tunable.
_CACHE_TTL_S = float(os.getenv("SEC_EDGAR_CACHE_TTL_S", "3600") or "3600")
# Bound the per-CIK facts cache so a long-running worker screening many deals does
# not grow it without limit; the oldest entry is evicted past this size. Env-tunable.
_MAX_CACHED_FACTS = int(os.getenv("SEC_EDGAR_MAX_CACHED_FACTS", "512") or "512")
_COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

# Canonical claim attribute -> EDGAR us-gaap concept candidates, most-specific
# first. Kept deliberately small; extend as concepts are validated against real
# filings.
_CONCEPTS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ),
    "net_income": ("NetIncomeLoss",),
    "total_assets": ("Assets",),
    "total_liabilities": ("Liabilities",),
    "total_equity": ("StockholdersEquity",),
    "cash_and_equivalents": ("CashAndCashEquivalentsAtCarryingValue",),
}

# Relative tolerance for "the same figure". Tight on purpose -- EDGAR XBRL is a
# filed exact number, so a genuine match is near-identical; past this is a
# material discrepancy worth surfacing. Tunable alongside the B2/B3 rules.
_REL_TOLERANCE = 0.005  # 0.5%

Fetch = Callable[[str], Awaitable[Any]]
Resolve = Callable[[AsyncSession, uuid.UUID], Awaitable["DealEntity | None"]]


async def _default_fetch(url: str) -> Any:
    async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


def _claim_usd_value(claim: Claim) -> float | None:
    """The claim's comparable USD figure, or None if it isn't one. EDGAR
    us-gaap facts are absolute USD, so anything whose currency or magnitude is
    not pinned down is no-signal -- never a forced comparison that would
    manufacture a delta out of a unit or scale we could not establish."""
    value = claim.value or {}
    normalized = value.get("normalized")
    if not isinstance(normalized, (int, float)) or isinstance(normalized, bool):
        return None
    # Currency must be explicitly USD. A missing unit is unknown currency, not an
    # implicit USD -- in a Canadian-market product a CAD figure compared against
    # EDGAR's USD would be a false conflict.
    if value.get("unit") != "USD":
        return None
    # `assumed_1x` means the scale was never detected, so `normalized` may be off
    # by 10^3/10^6 (a "$15,295" that was really in thousands). Against an absolute
    # EDGAR figure that is a fabricated delta -- decline (see contracts/claims.schema.json).
    if value.get("scale_source") == "assumed_1x":
        return None
    return float(normalized)


def _year_of(value: Any) -> int | None:
    """The calendar year of an EDGAR ISO date ("2023-12-31" -> 2023), or None."""
    if isinstance(value, str) and len(value) >= 4 and value[:4].isdigit():
        return int(value[:4])
    return None


def _covers_annual_period(unit: dict, year: int) -> bool:
    """Whether this datapoint covers the full fiscal year ending in `year`.

    The period MUST be read from the value's own `start`/`end`, never from the
    filing's `fy`/`fp`: `fy` is the DEI cover-page year of the filing, and a
    single 10-K (fy=2023) carries the 2022 and 2021 comparatives tagged with that
    same `fy:2023, fp:"FY"`. Keying on `fy` therefore pulls prior-year numbers
    into the candidate set for 2023 and can compare a correct 2023 claim against
    the 2021 figure -- a false, sticky `conflicted`.

    - `end` must fall in `year`.
    - A duration value (income-statement concept, has `start`) must span roughly a
      full year, so a quarter or half-year never stands in for the annual figure.
    - An instant value (balance-sheet concept, no `start`) at the fiscal year-end
      qualifies on its `end` year alone.
    """
    if _year_of(unit.get("end")) != year:
        return False
    start = unit.get("start")
    if start is None:
        return True  # instant value at fiscal year-end
    try:
        span = (date.fromisoformat(unit["end"]) - date.fromisoformat(start)).days
    except (ValueError, TypeError):
        return False
    return span >= 350  # ~a full year, excluding interim periods


def _lookup_annual_fact(
    facts: Any, concepts: tuple[str, ...], year: int
) -> tuple[str, float] | None:
    """The annual (10-K preferred) USD value for the first concept that reports
    one covering `year`. Returns (concept_name, value) or None.

    Candidates are restricted to datapoints whose OWN period covers `year`
    (see _covers_annual_period), so prior-year comparatives in the same filing
    are excluded before any tie-break. Among what remains -- typically the value
    as first filed and as re-reported in later filings -- 10-K wins over other
    forms and the latest-filed value wins (restatements supersede), with `end`
    then `frame` as stable, deterministic tiebreakers."""
    usgaap = (((facts or {}).get("facts") or {}).get("us-gaap")) or {}
    for concept in concepts:
        units = ((usgaap.get(concept) or {}).get("units") or {}).get("USD") or []
        annual = [u for u in units if isinstance(u, dict) and _covers_annual_period(u, year)]
        # The 10-K family, matched by prefix, not the exact string. A 10-K/A is
        # an AMENDED annual report -- the very filing a company uses to restate a
        # wrong number -- and 10-K405 / 10-KSB are older variants; excluding them
        # would let a stale original 10-K win over the amendment that corrects it,
        # undercutting "the latest-filed value supersedes". The latest-filed
        # tiebreak below then lets the 10-K/A supersede the original it amends.
        tens = [u for u in annual if str(u.get("form") or "").startswith("10-K")]
        candidates = tens or annual
        if not candidates:
            continue
        best = max(
            candidates,
            key=lambda u: (u.get("filed") or "", u.get("end") or "", u.get("frame") or ""),
        )
        val = best.get("val")
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return concept, float(val)
    return None


class SecEdgarSource:
    """CorroborationSource for SEC EDGAR XBRL company facts. Inject `fetch` in
    tests; the default hits data.sec.gov with the required User-Agent."""

    name = "sec_edgar"

    def __init__(self, fetch: Fetch | None = None, resolve: Resolve | None = None) -> None:
        self._fetch = fetch or _default_fetch
        # The deal-scoped resolved entity carries the SEC CIK the resolver already
        # settled; check() reads the CIK from there, never from claim.entity.
        # Injectable for tests.
        self._resolve = resolve or load_resolved_entity
        # CIK -> (fetched_at, companyfacts | None). run_corroboration calls check()
        # once PER claim, so without a cache a deal's dozen-plus financial claims
        # each re-download the same (large) facts file and blow past SEC's
        # fair-access limit. The TTL bounds staleness so a refiled company is
        # picked up without a worker restart; a failed/absent fetch is cached too,
        # so a bad CIK is attempted once per TTL, not once per claim.
        self._facts: dict[int, tuple[float, dict[str, Any] | None]] = {}

    async def _company_facts(self, cik: int) -> dict[str, Any] | None:
        """This CIK's XBRL company facts, fetched at most once per CIK per
        _CACHE_TTL_S (see self._facts). A failed/absent fetch is cached as None so
        a bad or unreachable CIK is attempted once per TTL, not once per claim --
        best-effort corroboration, and the bound on SEC requests matters more than
        retrying a transient blip within a single run; the TTL still lets a
        refiled company refresh without a worker restart."""
        cached = self._facts.get(cik)
        if cached is None or (time.monotonic() - cached[0]) > _CACHE_TTL_S:
            try:
                facts: dict[str, Any] | None = await self._fetch(_COMPANY_FACTS_URL.format(cik=cik))
            except Exception:
                logger.exception("EDGAR companyfacts fetch failed for CIK %s; no-signal", cik)
                facts = None
            # Bound the cache: evict the oldest entry before adding a new CIK once
            # at capacity, so a long-running worker cannot grow it without limit.
            if cik not in self._facts and len(self._facts) >= _MAX_CACHED_FACTS:
                oldest = min(self._facts, key=lambda c: self._facts[c][0])
                del self._facts[oldest]
            self._facts[cik] = (time.monotonic(), facts)
        return self._facts[cik][1]

    async def check(self, db: Any, claim: Claim) -> CorroborationVerdict | None:
        concepts = _CONCEPTS.get(claim.attribute)
        if concepts is None or claim.period_year is None:
            return None  # not an attribute/period EDGAR can speak to
        claim_value = _claim_usd_value(claim)
        if claim_value is None:
            return None  # nothing comparable (non-USD / non-numeric)

        # Key on the deal's RESOLVED CIK, never on claim.entity (see module
        # docstring / entity_resolution.resolved): no resolved entity, or no SEC
        # CIK resolved for it, is a clean no-signal -- never a name-matched guess.
        resolved = await self._resolve(db, claim.deal_id)
        if resolved is None:
            return None
        cik_str = resolved.registry_id(REGISTRY_CIK)
        if cik_str is None:
            return None  # SEC has not resolved a CIK for this deal -> no-signal
        cik = int(cik_str)

        facts = await self._company_facts(cik)
        if facts is None:
            return None  # fetch failed/absent -> no-signal

        found = _lookup_annual_fact(facts, concepts, claim.period_year)
        if found is None:
            return None  # no reported fact for this concept/period -> no-signal
        concept, edgar_value = found

        delta = abs(edgar_value - claim_value) / max(abs(edgar_value), 1.0)
        result = {
            "source": self.name,
            "cik": cik,
            "concept": concept,
            "attribute": claim.attribute,
            "period_year": claim.period_year,
            "claim_value": claim_value,
            "edgar_value": edgar_value,
            "discrepancy_delta": delta,
        }
        return CorroborationVerdict(agrees=delta <= _REL_TOLERANCE, result=result)
