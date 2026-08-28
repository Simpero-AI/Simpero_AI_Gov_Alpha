"""SEC EDGAR entity resolution (SIM-262, DS-A-BGCHK-1).

Keyless: SEC asks only for a descriptive User-Agent naming a contact, and
rate-limits at ~10 req/s. No API key, so nothing here blocks on the
OpenCorporates / Companies House provisioning that DS-A-BGCHK-1's original
sub-task 1 gated on.

No SDK, injectable client -- same call and same reasoning as
app/services/embedding.py's VoyageEmbedder: two JSON GETs, httpx is already a
dependency, and the request/response contract stays testable with no network.

TWO-STAGE, because EDGAR has no company-name search endpoint:

  1. `company_tickers.json` -- the full ticker -> {cik, title} map (~10k rows,
     ~1 MB). Cached per process with a TTL, same idiom as the JWKS cache in
     app/core/security.py. This is the only way to get from a name to a CIK
     without full-text search.
  2. `submissions/CIK##########.json` -- confirms the candidate and yields the
     legal name and formerNames.

The second stage is not a formality. Stage 1 matches a *display title*; stage
2 is the authoritative record. If they disagree, that is exactly the case
where guessing would anchor the deal to the wrong company, so it resolves to
`unresolved` instead.

MATCHING IS EXACT-AFTER-NORMALIZATION, deliberately. No fuzzy matching, no
edit distance, no "closest" candidate. Normalization only folds case,
punctuation, whitespace, and trailing legal-form tokens (INC, LLC, ...) --
the parts of a company name that carry no identity. It never touches
semantic words: "ACME HOLDINGS" and "ACME" are different companies and must
stay that way, and a scoring threshold here would be a knob tuned against
whatever sample happened to be at hand.

FOUND/NOT-FOUND COMES FROM THE BODY, NOT THE STATUS CODE. Registries return
success on not-found (the ticket names ISED returning HTTP 200); EDGAR
returns 404 for an unknown CIK but also serves 200 bodies that carry no
`cik`. Both are read as `not_found` by inspecting the payload.

KNOWN LIMITATION -- former names are a GUARD here, not a search key.
`company_tickers.json` carries only each filer's CURRENT title, so a company
that has since been renamed cannot be *looked up* by its old name: a deal
named "Facebook Inc" resolves to `not_found`, not to Meta's CIK. What
formerNames actually buys us is the protective half the ticket asks for --
"an old document isn't matched to the wrong current entity" -- in two ways:

  - stage 2 accepts a match when SEC's ticker file still carries the OLD
    title (it lags renames) but the submissions record has already moved on;
  - stage 2 REFUSES when the two disagree and no former name bridges them.

Looking a former name up directly needs EDGAR full-text search
(efts.sec.gov) or the browse-edgar company endpoint, neither of which is
wired here. That is a deliberate scope line: SIM-408's sub-task 1 already
names full-text search as its own concern, and adding a second, HTML-shaped
lookup path to hit an uncommon case is not worth the brittleness while the
conservative answer (`not_found`) is a safe one.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

from app.services.entity_resolution.types import (
    EntityResolutionError,
    FormerName,
    Resolution,
)

SOURCE = "sec_edgar"

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

# 10s, not embedding.py's 30s: this one runs inside a request's open
# transaction (see the endpoint docstring in app/api/deals.py), so the ceiling
# is also the longest a PgBouncer slot can be pinned by a slow registry.
_TIMEOUT = httpx.Timeout(10.0)

_TICKERS_TTL_SECONDS = 3600
_tickers_cache: dict[str, list[dict]] | None = None
_tickers_fetched_at: float = 0.0

# Trailing legal-form tokens only. Every entry names a *corporate form*, not
# part of a company's identity. Nothing semantic belongs here -- adding
# HOLDINGS, GROUP, PARTNERS or LABS would merge genuinely distinct filers.
_LEGAL_SUFFIXES = frozenset(
    {
        "INC",
        "INCORPORATED",
        "CORP",
        "CORPORATION",
        "CO",
        "COMPANY",
        "LLC",
        "LP",
        "LLP",
        "LTD",
        "LIMITED",
        "PLC",
        "NV",
        "SA",
        "AG",
        "GMBH",
    }
)

_PUNCT = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """Fold a company name to its identity-bearing core.

    Case, punctuation, and repeated whitespace go, then trailing legal-form
    tokens are stripped repeatedly -- "Acme Co., Inc." and "ACME COMPANY
    INCORPORATED" are the same filer, and both normalize to "ACME".

    Only TRAILING tokens are stripped: "CO" inside "CO OPERATIVE BANK" is not
    a suffix, and dropping it mid-name would corrupt the identity this
    function exists to preserve.
    """
    folded = _PUNCT.sub(" ", name).upper()
    tokens = _WHITESPACE.sub(" ", folded).strip().split(" ")
    while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _pad_cik(cik: object) -> str:
    """EDGAR's canonical 10-digit zero-padded CIK. The tickers map carries it
    as a bare int; every downstream URL and citation wants the padded form, so
    it is normalized once, here."""
    return f"{int(str(cik).strip()):010d}"


def _reset_tickers_cache() -> None:
    """Test seam -- the module-level cache would otherwise leak one test's
    fixture map into the next."""
    global _tickers_cache, _tickers_fetched_at
    _tickers_cache = None
    _tickers_fetched_at = 0.0


class EdgarResolver:
    """`Resolver` backed by SEC EDGAR.

    `user_agent` is required and has no default: SEC rejects unidentified
    traffic, and a silently-empty header would surface as a puzzling 403 at
    call time rather than a clear misconfiguration at construction. Same
    fail-closed posture as VoyageEmbedder's empty-api_key check.
    """

    def __init__(self, *, user_agent: str, client: httpx.AsyncClient | None = None) -> None:
        if not user_agent:
            raise EntityResolutionError(
                "SEC_EDGAR_USER_AGENT is not set, so entity resolution cannot run. SEC "
                "requires a descriptive User-Agent naming a contact (e.g. "
                "'Simpero AI ops@simpero.ai'). Set it in the environment, never in code."
            )
        self._user_agent = user_agent
        self._client = client

    @property
    def source(self) -> str:
        return SOURCE

    async def resolve(self, name: str) -> Resolution:
        """Resolve one company name to a CIK anchor. See the module docstring
        for the two stages and why matching is exact-after-normalization."""
        query = normalize_name(name)
        if not query:
            return Resolution(
                status="unresolved",
                source=SOURCE,
                query_name=name,
                reason="The deal name is empty once normalized, so there is nothing to search.",
                evidence={"normalized_query": query},
            )

        client = self._client or httpx.AsyncClient(timeout=_TIMEOUT)
        owns_client = self._client is None
        try:
            index = await self._ticker_index(client)
            candidates = index.get(query, [])

            if not candidates:
                return Resolution(
                    status="not_found",
                    source=SOURCE,
                    query_name=name,
                    reason="No SEC filer matches this name. Expected for a private company.",
                    evidence={"normalized_query": query, "candidates": 0, "stage": "ticker_map"},
                )

            ciks = {c["cik"] for c in candidates}
            if len(ciks) > 1:
                # Ambiguity is a stop, not a tie to break. Picking the first,
                # the lowest CIK, or the most tickers would each be a guess
                # dressed as a rule.
                return Resolution(
                    status="unresolved",
                    source=SOURCE,
                    query_name=name,
                    reason=(
                        f"{len(ciks)} different SEC filers share this name; "
                        "resolving one would be a guess."
                    ),
                    evidence={
                        "normalized_query": query,
                        "candidates": len(ciks),
                        "candidate_ciks": sorted(ciks),
                        "stage": "ticker_map",
                    },
                )

            cik = next(iter(ciks))
            return await self._confirm(client, cik, name=name, query=query, candidates=candidates)
        finally:
            if owns_client:
                await client.aclose()

    async def _confirm(
        self,
        client: httpx.AsyncClient,
        cik: str,
        *,
        name: str,
        query: str,
        candidates: list[dict],
    ) -> Resolution:
        """Stage 2: check the candidate against its authoritative submissions
        record, and read the legal name plus former names off it."""
        record = await self._submissions(client, cik)
        tickers = sorted({c["ticker"] for c in candidates if c.get("ticker")})
        evidence: dict[str, Any] = {
            "normalized_query": query,
            "candidates": 1,
            "cik": cik,
            "tickers": tickers,
            "endpoints": [_TICKERS_URL, _SUBMISSIONS_URL.format(cik=cik)],
            "stage": "submissions",
        }

        # Body, not status: a 200 with no `cik` is as much a not-found as a
        # 404, and treating it as a hit would anchor the deal to an empty
        # record.
        if record is None or not record.get("cik"):
            return Resolution(
                status="not_found",
                source=SOURCE,
                query_name=name,
                reason=(
                    f"The ticker map points at CIK {cik}, but SEC holds no submissions "
                    "record for it."
                ),
                evidence=evidence,
            )

        legal_name = str(record.get("name") or "").strip()
        former_names = _former_names(record)

        if normalize_name(legal_name) == query:
            evidence["matched_name"] = legal_name
            return Resolution(
                status="resolved",
                source=SOURCE,
                query_name=name,
                registry_id=cik,
                legal_name=legal_name,
                former_names=former_names,
                matched_on="current_name",
                evidence=evidence,
            )

        for former in former_names:
            if normalize_name(former.name) == query:
                # A real match, not a weaker one -- but the rename is itself a
                # fact about the deal, so the matched name and its window are
                # recorded for SIM-409 to pick up as a potential undisclosed
                # finding.
                evidence["matched_name"] = former.name
                evidence["matched_former_name"] = former.to_json()
                return Resolution(
                    status="resolved",
                    source=SOURCE,
                    query_name=name,
                    registry_id=cik,
                    legal_name=legal_name,
                    former_names=former_names,
                    matched_on="former_name",
                    evidence=evidence,
                )

        # The two SEC endpoints disagree with each other. Anchoring on that is
        # precisely the "wrong entity poisons every downstream check" failure.
        evidence["submissions_name"] = legal_name
        evidence["submissions_former_names"] = [f.to_json() for f in former_names]
        return Resolution(
            status="unresolved",
            source=SOURCE,
            query_name=name,
            reason=(
                f"SEC's ticker map matched this name to CIK {cik}, but that filer's "
                f"record names it {legal_name!r} with no matching former name."
            ),
            evidence=evidence,
        )

    async def _ticker_index(self, client: httpx.AsyncClient) -> dict[str, list[dict]]:
        """Normalized-title -> candidate rows, built once per TTL.

        Built as an index rather than scanned per call: the map is ~10k rows
        and a resolve would otherwise be a linear scan with a normalize() on
        every row.
        """
        global _tickers_cache, _tickers_fetched_at
        fresh = time.monotonic() - _tickers_fetched_at < _TICKERS_TTL_SECONDS
        if _tickers_cache is not None and fresh:
            return _tickers_cache

        payload = await self._get_json(client, _TICKERS_URL)
        if not isinstance(payload, dict):
            raise EntityResolutionError(
                f"SEC ticker map returned {type(payload).__name__}, expected an object"
            )

        index: dict[str, list[dict]] = {}
        for row in payload.values():
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            cik_raw = row.get("cik_str")
            if not title or cik_raw is None:
                continue
            key = normalize_name(title)
            if not key:
                continue
            index.setdefault(key, []).append(
                {
                    "cik": _pad_cik(cik_raw),
                    "ticker": str(row.get("ticker") or "").strip(),
                    "title": title,
                }
            )

        if not index:
            raise EntityResolutionError("SEC ticker map came back empty")

        _tickers_cache = index
        _tickers_fetched_at = time.monotonic()
        return index

    async def _submissions(self, client: httpx.AsyncClient, cik: str) -> dict | None:
        """The filer's submissions record, or None when SEC holds none.

        A 404 is an answer ("no such filer"), not a failure -- it is the one
        non-2xx that must not raise.
        """
        url = _SUBMISSIONS_URL.format(cik=cik)
        try:
            response = await client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise EntityResolutionError(f"SEC request failed: {exc}") from exc

        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise EntityResolutionError(
                f"SEC returned {response.status_code} for {url}: {response.text[:200]}"
            )
        return _parse_json(response, url)

    async def _get_json(self, client: httpx.AsyncClient, url: str) -> Any:
        try:
            response = await client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise EntityResolutionError(f"SEC request failed: {exc}") from exc
        if response.status_code != 200:
            raise EntityResolutionError(
                f"SEC returned {response.status_code} for {url}: {response.text[:200]}"
            )
        return _parse_json(response, url)

    def _headers(self) -> dict[str, str]:
        # SEC serves 403 to unidentified clients; Accept-Encoding is their
        # documented request for the large static files.
        return {"User-Agent": self._user_agent, "Accept-Encoding": "gzip, deflate"}


def _parse_json(response: httpx.Response, url: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise EntityResolutionError(f"SEC returned a non-JSON body for {url}") from exc


def _former_names(record: dict) -> tuple[FormerName, ...]:
    """EDGAR's `formerNames`, with its date windows preserved.

    Defensive about shape because this history is thin and inconsistent on
    older filers: a row with no usable name is dropped rather than stored as
    an empty former name, and missing dates stay None rather than being
    inferred from neighbouring rows.
    """
    raw = record.get("formerNames")
    if not isinstance(raw, list):
        return ()
    names: list[FormerName] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        names.append(
            FormerName(
                name=name,
                from_date=(str(entry["from"]) if entry.get("from") else None),
                to_date=(str(entry["to"]) if entry.get("to") else None),
            )
        )
    return tuple(names)
