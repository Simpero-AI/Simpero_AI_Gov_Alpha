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

Registered in corroboration_sources.DEFAULT_SOURCES and run by the corroboration
job (start_deal_corroboration) in its own phase, outside the verify transaction,
so a network call never sits unresolved inside a held transaction.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import httpx

from app.models.claim import Claim
from app.services.corroboration import CorroborationVerdict
from app.services.subject_fold import strip_legal_suffix

logger = logging.getLogger(__name__)

# SEC requires a descriptive User-Agent and rate-limits ~10 req/s; the caller
# (the corroboration pass) owns cross-call rate-limiting when this is registered.
_USER_AGENT = "Simpero corroboration (engineering@simpero.com)"
_TIMEOUT = 10.0
_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

# Well-known brand -> SEC-registrant aliases, in strip_legal_suffix core form (the
# same normalized space _resolve_cik matches in: lower-cased, legal suffix removed).
# These are EXACT curated facts -- a brand a deal is commonly named by whose SEC
# filer is a differently-named legal entity -- NOT fuzzy matching: a filing is
# Google's but the registrant is "Alphabet Inc."; Facebook files as "Meta
# Platforms". Without this a deal named by the brand ("google") matches no SEC
# title and every EDGAR corroboration verdict is silently 0. Extend deliberately:
# every entry must be an unambiguous brand -> registrant fact, never a guess.
_BRAND_ALIASES: dict[str, str] = {
    "google": "alphabet",
    "facebook": "meta platforms",
    "meta": "meta platforms",
}

# Canonical claim attribute -> EDGAR us-gaap concept candidates, most-specific
# first. Every tag is a standard us-gaap concept; candidates are tried in order so
# a filer using an older/alternate tag still resolves. Sign- or definition-
# ambiguous lines (see _CONFIRM_ONLY_ATTRIBUTES) confirm on a match but never
# conflict, so a benign like-vs-unlike comparison cannot manufacture a false
# conflict.
_CONCEPTS: dict[str, tuple[str, ...]] = {
    # --- Income statement (duration facts) ---
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ),
    "cogs": ("CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"),
    "gross_profit": ("GrossProfit",),
    "opex": ("OperatingExpenses",),
    "interest_expense": ("InterestExpense", "InterestExpenseNonoperating"),
    "tax_expense": ("IncomeTaxExpenseBenefit",),
    "depreciation_and_amortization": (
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
    ),
    "net_income": ("NetIncomeLoss",),
    # --- Balance sheet (instant facts) ---
    "total_assets": ("Assets",),
    "current_assets": ("AssetsCurrent",),
    "total_liabilities": ("Liabilities",),
    "current_liabilities": ("LiabilitiesCurrent",),
    "total_equity": ("StockholdersEquity",),
    "cash_and_equivalents": ("CashAndCashEquivalentsAtCarryingValue",),
    "accounts_receivable": ("AccountsReceivableNetCurrent", "ReceivablesNetCurrent"),
    "accounts_payable": ("AccountsPayableCurrent", "AccountsPayableTradeCurrent"),
    "inventory": ("InventoryNet",),
    # --- Cash flow (duration facts) ---
    "operating_cash_flow": (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment",),
}

# Relative tolerance for "the same figure". Tight on purpose -- EDGAR XBRL is a
# filed exact number, so a genuine match is near-identical; past this is a
# material discrepancy worth surfacing. Tunable alongside the B2/B3 rules.
_REL_TOLERANCE = 0.005  # 0.5%

# Attributes that have legitimate same-(attribute, entity, period_year) SIBLINGS
# the extraction does not disambiguate: an income-statement flow arrives as
# attribute="revenue" whether it is the consolidated total, a product/services
# line, a reportable segment, or a quarterly/interim figure -- all for the same
# fiscal year. EDGAR only knows the CONSOLIDATED ANNUAL total (see _CONCEPTS), so
# every sub-line or interim figure differs from it for an entirely benign reason.
# For these attributes we therefore CONFIRM on a match but stay no-signal (never
# `conflicted`) on a mismatch, so a product-revenue line is not flipped to
# `conflicted` merely for not equalling total revenue. Balance-sheet totals
# (assets/liabilities/equity/cash) are single consolidated values, so a real
# mismatch there is still surfaced as a conflict. The durable fix is finer
# extraction (tag total vs product vs services vs segment, and the period_kind),
# after which these can compare like-for-like against their own concepts; this is
# the display-safe gate until then.
_CONFIRM_ONLY_ATTRIBUTES = frozenset(
    {
        "revenue",
        "net_income",
        # Each has a benign non-match cause the extraction does not disambiguate, so
        # a mismatch is a like-vs-unlike comparison, not a real conflict: cogs/capex
        # sign convention varies (a deck may carry -220B while EDGAR reports +220B),
        # opex definitions differ (with/without COGS; R&D vs SG&A sub-lines),
        # interest_expense splits operating/nonoperating, tax_expense may be a
        # benefit (sign flip), and D&A is tagged several ways and split across the
        # cash-flow statement and segments. Confirm on a match, no-signal otherwise.
        # The clean single-consolidated stocks (gross_profit, current_assets/
        # liabilities, accounts_receivable/payable, inventory, operating_cash_flow)
        # stay FULL, so a genuinely wrong balance-sheet/OCF figure still conflicts.
        "cogs",
        "opex",
        "interest_expense",
        "tax_expense",
        "depreciation_and_amortization",
        "capex",
    }
)

Fetch = Callable[[str], Awaitable[Any]]


async def _default_fetch(url: str) -> Any:
    async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


_USD_UNITS = frozenset({"USD", "US$", "USD$", "$"})


def _claim_usd_value(claim: Claim, *, us_filer: bool = False) -> float | None:
    """The claim's comparable USD figure, or None if it isn't one. EDGAR
    us-gaap facts are absolute USD, so anything whose currency or magnitude is
    not pinned down is no-signal -- never a forced comparison that would
    manufacture a delta out of a unit or scale we could not establish.

    `us_filer` is passed only after the entity has resolved to a CIK in EDGAR's
    filer list -- i.e. it IS a US SEC registrant, which reports its 10-K in USD.
    There an *unlabeled* (or "$") figure is taken as USD -- a deck's top-line
    numbers are rarely tagged with a currency code -- while an *explicit* foreign
    currency is still declined: we relax the unknown-currency case, never a
    known-foreign one. Unresolved callers keep the strict rule (explicit USD only)."""
    value = claim.value or {}
    normalized = value.get("normalized")
    if not isinstance(normalized, (int, float)) or isinstance(normalized, bool):
        return None
    unit = value.get("unit")
    if us_filer:
        # A resolved US registrant reports in USD, so an unlabeled or "$" figure is
        # USD here; only an explicitly labelled non-USD currency is a real mismatch.
        if unit is not None and unit not in _USD_UNITS:
            return None
    elif unit != "USD":
        # Unresolved: currency must be explicitly USD. A missing unit is unknown
        # currency, not implicit USD -- in a Canadian-market product a CAD figure
        # compared against EDGAR's USD would be a false conflict.
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

    def __init__(self, fetch: Fetch | None = None) -> None:
        self._fetch = fetch or _default_fetch
        self._tickers: dict[str, int] | None = (
            None  # normalized title -> CIK; ambiguous titles dropped
        )
        self._by_ticker: dict[str, int] | None = (
            None  # normalized ticker symbol -> CIK; ambiguous tickers dropped
        )

    async def _resolve_cik(self, company_name: str) -> int | None:
        """Resolve a company name to a CIK against company_tickers.json, three
        ways, most-specific first:

        1. Ticker symbol -- a deal named/aliased by its symbol ("GOOGL") matches
           the row's `ticker` directly. Unambiguous by construction.
        2. Brand alias (_BRAND_ALIASES) -- a brand whose SEC registrant has a
           different legal name ("google" -> "alphabet", "facebook" -> "meta
           platforms"), applied in the same normalized core space as the title
           match below.
        3. Suffix-insensitive title -- both the SEC title and the claim entity are
           reduced by strip_legal_suffix, so a deck's bare "Snowflake" resolves to
           SEC's "Snowflake Inc." (the common case: a deck names a company without
           its legal form).

        Returns a CIK only on an unambiguous single match -- deterministic, never
        a guess; None when not found or ambiguous (a suffix-strip collision
        between two filers drops to no-signal, not a conflict). Two rows sharing a
        title AND CIK -- e.g. GOOGL and GOOG both filing as Alphabet -- are the
        same filer, so they are NOT ambiguous."""
        if self._tickers is None or self._by_ticker is None:
            try:
                data = await self._fetch(_COMPANY_TICKERS_URL)
            except Exception:
                logger.exception("EDGAR company_tickers fetch failed; treating as no-signal")
                return None
            rows = data.values() if isinstance(data, dict) else (data or [])
            titles: dict[str, int | None] = {}
            tickers: dict[str, int | None] = {}

            def _record(index: dict[str, int | None], key: str, cik: int) -> None:
                # Ambiguous (None) the moment a SECOND, DIFFERENT CIK claims the
                # key; a repeat of the same CIK (GOOGL/GOOG -> one Alphabet) keeps
                # it resolvable.
                index[key] = None if key in index and index[key] != cik else cik

            for row in rows:
                cik = row.get("cik_str")
                if not isinstance(cik, int):
                    continue
                title = strip_legal_suffix(str(row.get("title", "")))
                if title:
                    _record(titles, title, cik)
                ticker = strip_legal_suffix(str(row.get("ticker", "")))
                if ticker:
                    _record(tickers, ticker, cik)
            self._tickers = {t: c for t, c in titles.items() if c is not None}
            self._by_ticker = {t: c for t, c in tickers.items() if c is not None}

        core = strip_legal_suffix(company_name)
        # A CIK is always a positive int, so `or` safely falls through a miss.
        return self._by_ticker.get(core) or self._tickers.get(_BRAND_ALIASES.get(core, core))

    async def check(self, db: Any, claim: Claim) -> CorroborationVerdict | None:
        concepts = _CONCEPTS.get(claim.attribute)
        if concepts is None or claim.period_year is None:
            return None  # not an attribute/period EDGAR can speak to

        cik = await self._resolve_cik(claim.entity)
        if cik is None:
            return None  # not an EDGAR filer, or ambiguous -> no-signal

        # The entity resolved to a CIK, so it is a US SEC registrant reporting in
        # USD: an unlabeled figure is taken as USD here (a foreign currency is
        # still declined inside), which is what lets the many unlabeled USD
        # top-line figures corroborate instead of being silently dropped.
        claim_value = _claim_usd_value(claim, us_filer=True)
        if claim_value is None:
            return None  # nothing comparable (explicit non-USD / non-numeric / unknown scale)

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
        agrees = delta <= _REL_TOLERANCE

        # A mismatch on a sub-line-ambiguous flow (revenue / net_income) is almost
        # always a benign like-vs-total comparison -- a product/services line, a
        # segment, or a quarter measured against EDGAR's consolidated annual total.
        # Emitting agrees=False there manufactures a false conflict and flips the
        # claim to `conflicted`, so we decline (no-signal) instead of conflicting.
        # A genuine match still confirms; balance-sheet totals still conflict.
        if not agrees and claim.attribute in _CONFIRM_ONLY_ATTRIBUTES:
            logger.info(
                "EDGAR: %s claim %s != consolidated %s %s for FY%s -- sub-line/interim "
                "ambiguity, no-signal (not a conflict)",
                claim.attribute,
                claim_value,
                concept,
                edgar_value,
                claim.period_year,
            )
            return None

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
        return CorroborationVerdict(agrees=agrees, result=result)
