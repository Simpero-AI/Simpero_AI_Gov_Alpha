"""SEC EDGAR corroboration source (Epic 12) — the first real CorroborationSource.

Given a financial claim, resolve its company to a CIK and compare the claim's
figure to EDGAR's XBRL company facts for the same period. It agrees within a
tight tolerance, disagrees on a material delta (recording both values + the
delta so the conflict view can show them), and returns no-signal (None) for
everything it cannot compare -- company not an EDGAR filer, an attribute it does
not map, no reported fact for that period, or a non-USD unit. Absence is never a
conflict (handover surfacing rule 9.3.5).

Deterministic by design (handover C-10/C-11): name -> CIK is an exact normalized
match against EDGAR's company_tickers.json, and the roll-up never sees a
model-derived value. The fuzzy / name-history AI-propose seam (handover 5.1) and
former-name resolution are a follow-up, not this first cut.

NOT registered in app.services.corroboration.CORROBORATION_SOURCES yet: it goes
live only once the corroboration pass's I/O placement is settled (SIM-253), so a
network call never sits unresolved inside the verify transaction.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.models.claim import Claim
from app.services.corroboration import CorroborationVerdict

logger = logging.getLogger(__name__)

# SEC requires a descriptive User-Agent and rate-limits ~10 req/s; the caller
# (the corroboration pass) owns cross-call rate-limiting when this is registered.
_USER_AGENT = "Simpero corroboration (engineering@simpero.com)"
_TIMEOUT = 10.0
_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
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


async def _default_fetch(url: str) -> Any:
    async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


def _normalize(name: str) -> str:
    return " ".join(name.casefold().split())


def _claim_usd_value(claim: Claim) -> float | None:
    """The claim's comparable USD figure, or None if it isn't one. EDGAR
    us-gaap facts are USD, so a non-USD (or non-numeric) claim has nothing
    comparable -- no-signal, never a forced comparison across units."""
    value = claim.value or {}
    normalized = value.get("normalized")
    if not isinstance(normalized, (int, float)) or isinstance(normalized, bool):
        return None
    unit = value.get("unit")
    if unit not in (None, "USD"):
        return None
    return float(normalized)


def _lookup_annual_fact(
    facts: Any, concepts: tuple[str, ...], year: int
) -> tuple[str, float] | None:
    """The annual (fiscal-year, 10-K preferred) USD value for the first concept
    that reports one for `year`. Returns (concept_name, value) or None. On
    restatements the latest-filed value wins."""
    usgaap = (((facts or {}).get("facts") or {}).get("us-gaap")) or {}
    for concept in concepts:
        units = ((usgaap.get(concept) or {}).get("units") or {}).get("USD") or []
        annual = [u for u in units if u.get("fy") == year and u.get("fp") == "FY"]
        tens = [u for u in annual if u.get("form") == "10-K"]
        candidates = tens or annual
        if not candidates:
            continue
        best = max(candidates, key=lambda u: u.get("filed", ""))
        val = best.get("val")
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return concept, float(val)
    return None


class SecEdgarSource:
    """CorroborationSource for SEC EDGAR XBRL company facts. Inject `fetch` in
    tests; the default hits data.sec.gov with the required User-Agent."""

    name = "sec_edgar"

    def __init__(self, fetch: Fetch | None = None) -> None:
        self._fetch = fetch or _default_fetch
        self._tickers: dict[str, int] | None = (
            None  # normalized title -> CIK; ambiguous titles dropped
        )

    async def _resolve_cik(self, company_name: str) -> int | None:
        """Exact normalized-title match against company_tickers.json. Returns a
        CIK only on an unambiguous single match -- deterministic, never a guess;
        None when not found or ambiguous (both are no-signal, not a conflict)."""
        if self._tickers is None:
            try:
                data = await self._fetch(_COMPANY_TICKERS_URL)
            except Exception:
                logger.exception("EDGAR company_tickers fetch failed; treating as no-signal")
                return None
            rows = data.values() if isinstance(data, dict) else (data or [])
            seen: dict[str, int | None] = {}
            for row in rows:
                title = _normalize(str(row.get("title", "")))
                cik = row.get("cik_str")
                if not title or not isinstance(cik, int):
                    continue
                # Mark a title ambiguous (None) the moment a second CIK claims it.
                seen[title] = None if title in seen and seen[title] != cik else cik
            self._tickers = {t: c for t, c in seen.items() if c is not None}
        return self._tickers.get(_normalize(company_name))

    async def check(self, db: Any, claim: Claim) -> CorroborationVerdict | None:
        concepts = _CONCEPTS.get(claim.attribute)
        if concepts is None or claim.period_year is None:
            return None  # not an attribute/period EDGAR can speak to
        claim_value = _claim_usd_value(claim)
        if claim_value is None:
            return None  # nothing comparable (non-USD / non-numeric)

        cik = await self._resolve_cik(claim.entity)
        if cik is None:
            return None  # not an EDGAR filer, or ambiguous -> no-signal

        try:
            facts = await self._fetch(_COMPANY_FACTS_URL.format(cik=cik))
        except Exception:
            logger.exception("EDGAR companyfacts fetch failed for CIK %s; no-signal", cik)
            return None

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
