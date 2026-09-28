"""Web-search deep-search COLLECT pass (Epic 12 / SIM-419).

Actively searches the public web for market and company facts a deal's own
documents may not contain -- market sizing (TAM/SAM/SOM/market size/CAGR),
competitors, market definition, and company overview/risks/related-parties/plans
-- and mints them as *cited web claims* that flow through the same
build_market_view / build_company_view the document claims do. Every collected
fact carries the real source URL it came from (data_source.source_url), so the
Market and Company tabs cite the web, never a fabricated document reference.

Two phases, mirroring the corroboration engine's HTTP-outside-transaction
discipline:
  gather_web_facts(...)   -> the Anthropic web_search call + tool-based
                             adjudication into claim-shaped candidates. No DB.
  persist_web_facts(...)  -> mint the candidates as claims under synthetic
                             per-URL `web` data_source rows. A short write txn.

Guardrails: the Anthropic web_search server tool is given an `allowed_domains`
reputable allowlist (bounding *which* sites can be cited) and a `max_uses` cap
(bounding cost); the adjudicator independently re-checks every source URL is
https and on the allowlist (defence in depth -- a model must not smuggle a fact
in from an off-allowlist page). Fails soft on every axis: no API key, a
model/transport error, or no usable facts all yield an empty list, and the
corroboration job simply proceeds.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.claim import Claim
from app.models.data_source import DataSource

if TYPE_CHECKING:
    from anthropic.types import ToolParam

logger = logging.getLogger(__name__)

# Reputable public sources the web_search tool may cite. Market-research houses,
# authoritative registries/press -- deliberately excludes LinkedIn/Crunchbase
# (out per the provider decision) and anything user-generated.
#
# CRITICAL: every domain here must be crawlable by Anthropic's web_search user
# agent. The tool rejects the WHOLE request with a 400 ("The following domains
# are not accessible to our user agent: ...") if ANY allowed_domain is
# inaccessible -- so one blocked domain silently zeroes out every search for
# every deal. reuters.com / wsj.com / ft.com were removed for exactly that (they
# block the crawler); verify a new domain is crawler-accessible before adding it.
# Tunable; passed to the web_search tool's allowed_domains AND re-checked in the
# adjudicator.
DEFAULT_ALLOWED_DOMAINS: tuple[str, ...] = (
    "grandviewresearch.com",
    "mordorintelligence.com",
    "marketsandmarkets.com",
    "statista.com",
    "ibisworld.com",
    "gartner.com",
    "forrester.com",
    "mckinsey.com",
    "bloomberg.com",
    "sec.gov",
    "businesswire.com",
    "prnewswire.com",
    "techcrunch.com",
)

# Bounds the cost/latency of one deal's collect pass: the web_search tool will
# run at most this many searches inside the single Anthropic call.
_MAX_SEARCHES = 6
_LLM_TIMEOUT_S = 90.0
_MAX_TEXT_CHARS = 600
_MAX_FACTS = 40

# The competitor pass runs SEPARATELY from the general collect above, and on its own
# token budget, on purpose: the general call was reporting `sizing` first and hitting
# max_tokens (4096) before it ever got to the competitive-landscape section, so the
# Competitor tab was always empty even though the searches ran. A dedicated call with
# a competitor-only prompt and a larger output budget lets the model report the full
# 8-12 named competitors it finds (measured: 12 for NVIDIA, stop_reason=tool_use).
_COMPETITOR_MAX_SEARCHES = 8
_COMPETITOR_MAX_TOKENS = 8192

# section (model-facing) -> assertion_class (claims spine). Kept in lockstep with
# company_view/market_view's routing so a collected assertion lands in the right
# tab section.
_SECTION_TO_ASSERTION_CLASS: dict[str, str] = {
    "market_definition": "market_definition",
    "competitive_position": "competitive_position",
    "company_overview": "operating_model",
    "company_risks": "risk_or_dependency",
    "commercial_terms": "commercial_terms",
    "related_parties": "related_party",
    "plans": "plan_or_commitment",
}

# sizing metric (model-facing) -> (attribute_raw that _sizing_label matches,
# value_type the slot requires). See market_view._SIZING_LABELS.
_SIZING_METRIC: dict[str, tuple[str, str]] = {
    "TAM": ("TAM", "currency"),
    "SAM": ("SAM", "currency"),
    "SOM": ("SOM", "currency"),
    "market_size": ("market size", "currency"),
    "cagr": ("market growth", "percent"),
}


@dataclass(frozen=True)
class WebFactCandidate:
    """One adjudicated, allowlist-passed fact, already in claim shape. `entity`
    is the subject the claim is about (a market descriptor for sizing, the named
    competitor/subject for an assertion); `value` is the JSONB value payload;
    `source_url`/`source_title` become the synthetic web data_source."""

    claim_kind: str  # "quantitative" | "qualitative"
    assertion_class: str | None
    attribute: str
    attribute_raw: str | None
    entity: str
    value: dict[str, Any]
    source_url: str
    source_title: str


def _report_tool() -> ToolParam:
    return {
        "name": "report_web_facts",
        "description": (
            "Report the market and company facts found on the web, each grounded in a "
            "specific search-result source URL. Only report a fact that appears in a "
            "search result; never estimate or invent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sizing": {
                    "type": "array",
                    "description": "Numeric market-sizing figures for the company's market.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "metric": {
                                "type": "string",
                                "enum": ["TAM", "SAM", "SOM", "market_size", "cagr"],
                            },
                            "market": {
                                "type": "string",
                                "description": (
                                    "The market/industry the figure is about, "
                                    "e.g. 'US online gaming market'."
                                ),
                            },
                            "value_raw": {
                                "type": "string",
                                "description": "The figure as written, e.g. '$12.3B' or '8.4%'.",
                            },
                            "value_number": {
                                "type": "number",
                                "description": (
                                    "The figure as a plain number (dollars, or percent for cagr)."
                                ),
                            },
                            "unit": {
                                "type": "string",
                                "description": "Currency code/symbol, or null for cagr.",
                            },
                            "source_url": {"type": "string"},
                            "source_title": {"type": "string"},
                        },
                        "required": ["metric", "market", "value_raw", "value_number", "source_url"],
                    },
                },
                "assertions": {
                    "type": "array",
                    "description": "Qualitative market/company facts.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "section": {
                                "type": "string",
                                "enum": list(_SECTION_TO_ASSERTION_CLASS.keys()),
                            },
                            "subject": {
                                "type": "string",
                                "description": (
                                    "Who/what the assertion is about (a competitor, the market, "
                                    "the company)."
                                ),
                            },
                            "text": {
                                "type": "string",
                                "description": "The assertion, one concrete sentence.",
                            },
                            "source_url": {"type": "string"},
                            "source_title": {"type": "string"},
                        },
                        "required": ["section", "subject", "text", "source_url"],
                    },
                },
            },
            "required": ["sizing", "assertions"],
        },
    }


def _system_prompt() -> str:
    return (
        "You are a private-equity diligence analyst. Using web search, find factual, "
        "citable information about the target company and its market, then report it via "
        "report_web_facts.\n\n"
        "Spend dedicated searches on the market -- sizing, structure and growth -- "
        "which the target's own filings rarely cover well. (The competitive landscape "
        "is collected by a separate, dedicated pass, so you need not search for "
        "competitors here.)\n\n"
        "Collect:\n"
        "- Market sizing: TAM/SAM/SOM, overall market size, and market CAGR.\n"
        "- Market definition: what the market is, its structure, its main segments "
        "and buyer/customer types, and its growth drivers.\n"
        "- Company overview: what the company does and how it operates.\n"
        "- Commercial terms: key customers, pricing, and contract/renewal terms.\n"
        "- Risks, related parties, and stated plans, when publicly reported.\n\n"
        "Suggested searches (adapt to the company and sector): "
        "'<sector> market size and growth', '<sector> market segmentation', "
        "'<company> business model customers'.\n\n"
        "Hard rules:\n"
        "- Report ONLY facts that appear in a search result, each with the exact "
        "source URL it came from. Never estimate or invent a figure, name, or URL.\n"
        "- Prefer authoritative sources (market-research firms, regulators, major press).\n"
        "- If you find nothing citable for a category, omit it. Return empty "
        "arrays rather than padding."
    )


def _competitor_system_prompt() -> str:
    """The DEDICATED competitor pass -- reuses report_web_facts, but the model spends
    its whole budget on the competitive landscape (reported as `competitive_position`
    assertions), so the tab is filled with the full named set rather than truncated."""
    return (
        "You are a private-equity diligence analyst mapping the TARGET company's "
        "competitive landscape. Using web search, identify its direct and closest "
        "competitors, then report them via report_web_facts.\n\n"
        "Find the top 8-12 NAMED competitors. For EACH competitor, report ONE assertion "
        "with section='competitive_position', subject=<the competitor's name>, and text = "
        "one concrete sentence covering what it makes/does that competes with the target, "
        "how it is positioned or differentiated, and its market share or rank when "
        "publicly reported. Each assertion needs the exact source URL it came from.\n\n"
        "Run several targeted searches, e.g. '<company> competitors', '<company> vs "
        "<rival>', '<sector> market share leaders', '<sector> key players', "
        "'<sector> competitive landscape'.\n\n"
        "Hard rules:\n"
        "- Report ONLY competitors that appear in a search result, each with its exact "
        "source URL. Never invent a name or URL.\n"
        "- One assertion per competitor; put every competitor in the `assertions` array "
        "with section='competitive_position'. Leave `sizing` an empty array -- market "
        "sizing is collected elsewhere.\n"
        "- Prefer authoritative sources (market-research firms, major press, filings).\n"
        "- Return an empty array rather than padding with weak or unnamed rivals."
    )


def _domain(url: str) -> str | None:
    try:
        host = urlparse(url).netloc.lower()
    except (ValueError, TypeError):
        return None
    return host[4:] if host.startswith("www.") else host or None


def _url_allowed(url: Any, allowed: frozenset[str]) -> bool:
    """Only an https URL whose registrable host is on the allowlist (exact or a
    subdomain of one) passes -- so a model cannot cite an off-allowlist page even
    though the web_search tool was already domain-restricted."""
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    host = _domain(url)
    if host is None:
        return False
    return any(host == d or host.endswith("." + d) for d in allowed)


def _clean_text(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    out = " ".join(text.split())
    if not out or len(out) > _MAX_TEXT_CHARS:
        return None
    return out


_SCALE_BY_TOKEN: dict[str, float] = {
    "trillion": 1e12,
    "t": 1e12,
    "billion": 1e9,
    "bn": 1e9,
    "b": 1e9,
    "million": 1e6,
    "mm": 1e6,
    "m": 1e6,
    "thousand": 1e3,
    "k": 1e3,
}
_SIZING_SCALE_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(trillion|billion|million|thousand|bn|mm|[tbmk])\b",
    re.IGNORECASE,
)


def _sizing_normalized(value_raw: str, value_number: float) -> float:
    """Market-size figure in ABSOLUTE dollars. Models frequently return
    `value_number` as the MANTISSA of a scaled string ("$537.6B" -> 537.6),
    dropping the scale, which then renders as "$537.6" with the billions gone. So
    when `value_raw` carries a scale marker (billion/B, million/M, trillion/T,
    thousand/K), trust it: normalized = mantissa * scale -- but only override when
    `value_number` is clearly the un-scaled mantissa (>= ~100x smaller), so a model
    that already returned the full number is left alone. No scale marker (e.g. a
    CAGR "8.4%") -> value_number as given."""
    match = _SIZING_SCALE_RE.search(value_raw)
    if match is None:
        return float(value_number)
    mantissa = float(match.group(1).replace(",", ""))
    scaled = mantissa * _SCALE_BY_TOKEN[match.group(2).lower()]
    if value_number and scaled / abs(value_number) >= 100:
        return scaled
    return float(value_number)


def _adjudicate(raw: dict[str, Any], allowed: frozenset[str]) -> list[WebFactCandidate]:
    """Pure: turn the model's report_web_facts input into claim-shaped
    candidates, dropping anything whose source URL is not an allowlisted https
    link or whose value is unusable. No DB, no network."""
    candidates: list[WebFactCandidate] = []

    for item in raw.get("sizing") or []:
        if not isinstance(item, dict):
            continue
        metric = item.get("metric")
        mapped = _SIZING_METRIC.get(metric) if isinstance(metric, str) else None
        if mapped is None:
            continue
        attribute_raw, value_type = mapped
        url = item.get("source_url")
        if not _url_allowed(url, allowed):
            continue
        number = item.get("value_number")
        if not isinstance(number, (int, float)) or isinstance(number, bool):
            continue
        raw_value = _clean_text(item.get("value_raw")) or str(number)
        market = _clean_text(item.get("market")) or "the market"
        # Ensure the sizing entity reads as a market descriptor so it fills a
        # slot the target lacks (market_view._is_market_descriptor).
        entity = (
            market
            if any(t in market.lower() for t in ("market", "industry", "sector"))
            else f"{market} market"
        )
        candidates.append(
            WebFactCandidate(
                claim_kind="quantitative",
                assertion_class=None,
                attribute="operating_metric",
                attribute_raw=attribute_raw,
                entity=entity,
                value={
                    "raw": raw_value,
                    "normalized": (
                        _sizing_normalized(raw_value, number)
                        if value_type == "currency"
                        else float(number)
                    ),
                    "unit": _clean_text(item.get("unit")) if value_type == "currency" else None,
                    "value_type": value_type,
                },
                source_url=url,  # type: ignore[arg-type]  # _url_allowed proved it is a str
                source_title=_clean_text(item.get("source_title")) or (_domain(url) or url),  # type: ignore[arg-type]
            )
        )

    for item in raw.get("assertions") or []:
        if not isinstance(item, dict):
            continue
        section = item.get("section")
        assertion_class = (
            _SECTION_TO_ASSERTION_CLASS.get(section) if isinstance(section, str) else None
        )
        if assertion_class is None:
            continue
        url = item.get("source_url")
        if not _url_allowed(url, allowed):
            continue
        text = _clean_text(item.get("text"))
        if text is None:
            continue
        subject = _clean_text(item.get("subject"))
        if subject is None:
            # claims.entity is NOT NULL and a subject-less assertion is ambiguous
            # (whose competitive position? which related party?) -- drop it rather
            # than mint an entity-less claim (which would fail the INSERT).
            continue
        candidates.append(
            WebFactCandidate(
                claim_kind="qualitative",
                assertion_class=assertion_class,
                attribute="operating_metric",
                attribute_raw=None,
                entity=subject,
                value={"raw": text, "normalized": None, "unit": None, "value_type": "text"},
                source_url=url,  # type: ignore[arg-type]
                source_title=_clean_text(item.get("source_title")) or (_domain(url) or url),  # type: ignore[arg-type]
            )
        )

    return candidates[:_MAX_FACTS]


def _run_web_search(
    *,
    api_key: str,
    model: str,
    company: str,
    sector: str | None,
    allowed: tuple[str, ...],
    system: str,
    max_tokens: int,
    max_uses: int,
) -> dict[str, Any]:
    """Blocking Anthropic call with the web_search server tool + the report tool.
    The model searches (server-side, bounded by max_uses + allowed_domains) then
    calls report_web_facts; we return that tool input. Run via asyncio.to_thread.
    `system`/`max_tokens`/`max_uses` are passed so the general-collect and the
    dedicated competitor pass can share this plumbing with different budgets."""
    import anthropic

    # max_retries=0: this is best-effort enrichment that already fails soft to [],
    # so a single attempt is the right posture -- and it keeps the max_uses/timeout
    # cost bound honest (the SDK's default retries would multiply web searches).
    client = anthropic.Anthropic(api_key=api_key, timeout=_LLM_TIMEOUT_S, max_retries=0)
    web_search_tool: dict[str, Any] = {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": max_uses,
        "allowed_domains": list(allowed),
    }
    user = f"Target company: {company}" + (f"\nSector: {sector}" if sector else "")
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        # cast: the pinned SDK (1.2.0) has no typed param for the web_search
        # server tool, but the API accepts the raw tool dict -- it is passed
        # through verbatim. The report tool is a normal ToolParam.
        tools=cast("Any", [web_search_tool, _report_tool()]),
        messages=[{"role": "user", "content": user}],
        # temperature=0 for reproducibility: it stabilises the model's query choices
        # and how it adjudicates results into facts. The live web itself still varies
        # run-to-run, so this does not make web-collect fully deterministic -- the
        # durable fix is snapshotting the result once per analysis (persist wave) --
        # but it removes the model as an extra, avoidable source of drift. This SDK
        # build exposes no `temperature` kwarg, so it goes through extra_body (the
        # documented escape hatch that merges into the request body).
        extra_body={"temperature": 0},
    )
    for block in message.content:
        if getattr(block, "type", None) != "tool_use":
            continue
        if getattr(block, "name", None) != "report_web_facts":
            continue
        data = getattr(block, "input", None)
        if isinstance(data, dict):
            return data
    return {}


def _call_web_search(
    *, api_key: str, model: str, company: str, sector: str | None, allowed: tuple[str, ...]
) -> dict[str, Any]:
    """General market/company collect: the 5-arg call shape gather_web_facts (and its
    test injection) expect."""
    return _run_web_search(
        api_key=api_key,
        model=model,
        company=company,
        sector=sector,
        allowed=allowed,
        system=_system_prompt(),
        max_tokens=4096,
        max_uses=_MAX_SEARCHES,
    )


def _call_competitor_search(
    *, api_key: str, model: str, company: str, sector: str | None, allowed: tuple[str, ...]
) -> dict[str, Any]:
    """Dedicated competitor collect: same 5-arg shape, but the competitor prompt and a
    larger output budget so the full named set is reported, not truncated."""
    return _run_web_search(
        api_key=api_key,
        model=model,
        company=company,
        sector=sector,
        allowed=allowed,
        system=_competitor_system_prompt(),
        max_tokens=_COMPETITOR_MAX_TOKENS,
        max_uses=_COMPETITOR_MAX_SEARCHES,
    )


def _blocked_domains(err: Exception) -> tuple[str, ...]:
    """The allowed_domains named in a web_search "not accessible to our user agent"
    400, parsed from the error text; empty when the error is any other shape (so
    the caller re-raises it). The tool lists EVERY inaccessible domain in the one
    error, so a single parse gets them all."""
    msg = str(err)
    marker = msg.lower().find("not accessible")
    if marker == -1:
        return ()
    found = re.findall(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", msg[marker:], re.IGNORECASE)
    return tuple(dict.fromkeys(d.lower() for d in found))


async def _gather(
    *,
    company: str,
    sector: str | None,
    api_key: str,
    model: str,
    allowed_domains: Sequence[str],
    call: Any,
    label: str,
) -> list[WebFactCandidate]:
    """Shared collect core for both passes (general market/company facts and the
    dedicated competitor pass): run the web-search call, recover from a crawler-blocked
    allowlist 400 by dropping the named domains once, adjudicate, and fail soft to []
    on any error. `call` is a 5-arg web-search call (the real one or a test injection);
    `label` tags the log lines for the two passes."""
    if not api_key or not company:
        logger.info(
            "%s skipped for %r: reason=%s -- no web facts minted this run",
            label,
            company,
            "no_api_key" if not api_key else "no_company",
        )
        return []
    allowed = tuple(allowed_domains)
    try:
        try:
            raw = await asyncio.to_thread(
                call, api_key=api_key, model=model, company=company, sector=sector, allowed=allowed
            )
        except Exception as err:
            # The web_search tool 400s the ENTIRE request when it names any
            # allowed_domain its crawler can't reach -- so one gated research/press
            # domain (gartner/bloomberg/mckinsey are prone to it) would otherwise
            # silently zero every deal's search. Recover by dropping exactly the
            # named domains and retrying once with the rest; any other error
            # re-raises to the fail-soft handler below.
            blocked = _blocked_domains(err)
            reduced = tuple(d for d in allowed if d not in blocked)
            if not blocked or not reduced or len(reduced) == len(allowed):
                raise
            logger.warning(
                "%s for %r: dropping %d crawler-inaccessible domain(s) %s and retrying",
                label,
                company,
                len(allowed) - len(reduced),
                list(blocked),
            )
            allowed = reduced
            raw = await asyncio.to_thread(
                call, api_key=api_key, model=model, company=company, sector=sector, allowed=allowed
            )
        if not isinstance(raw, dict):
            logger.info("%s for %r: model returned no structured facts", label, company)
            return []
        # _adjudicate is inside the try too: an adjudication bug must also fail
        # soft to [] and never escape into the corroboration job's phase B.
        candidates = _adjudicate(raw, frozenset(allowed))
        # Observability: distinguishes "the model reported nothing" (web_search
        # found/cited nothing) from "reported N but the allowlist dropped them all"
        # (tune DEFAULT_ALLOWED_DOMAINS) from "N minted" -- the collect path is
        # otherwise silent on success, so an empty result is undiagnosable. A hard
        # allowlist 400 (one crawler-blocked domain zeroes the whole search) raises
        # and is caught below at WARNING; this catches the softer case where the
        # model cited facts but every URL fell outside the allowlist.
        n_sizing = len(raw.get("sizing") or [])
        n_assertions = len(raw.get("assertions") or [])
        if (n_sizing or n_assertions) and not candidates:
            logger.warning(
                "%s for %r: model reported %d facts but 0 passed the allowlist -- "
                "review DEFAULT_ALLOWED_DOMAINS (a blocked domain can also 400 the whole search)",
                label,
                company,
                n_sizing + n_assertions,
            )
        else:
            logger.info(
                "%s for %r: model reported %d sizing + %d assertions; %d passed the allowlist",
                label,
                company,
                n_sizing,
                n_assertions,
                len(candidates),
            )
        return candidates
    except Exception:
        logger.warning("%s failed for %r; returning no facts", label, company, exc_info=True)
        return []


async def gather_web_facts(
    *,
    company: str,
    sector: str | None,
    api_key: str,
    model: str,
    allowed_domains: Sequence[str] = DEFAULT_ALLOWED_DOMAINS,
    _call: Any = None,
) -> list[WebFactCandidate]:
    """Search the web for the deal's market/company facts and return adjudicated,
    allowlist-passed candidates. Fails soft to [] on any error. `_call` is an
    injection point for tests (defaults to the real Anthropic call)."""
    return await _gather(
        company=company,
        sector=sector,
        api_key=api_key,
        model=model,
        allowed_domains=allowed_domains,
        call=_call or _call_web_search,
        label="web-collect",
    )


async def gather_competitors(
    *,
    company: str,
    sector: str | None,
    api_key: str,
    model: str,
    allowed_domains: Sequence[str] = DEFAULT_ALLOWED_DOMAINS,
    _call: Any = None,
) -> list[WebFactCandidate]:
    """The DEDICATED competitor pass: a separate web-search call whose whole output
    budget goes to the competitive landscape, so the Competitor tab is filled with the
    full named set instead of being truncated by the general collect's sizing section.
    Returns `competitive_position` candidates (one per competitor); fails soft to []."""
    return await _gather(
        company=company,
        sector=sector,
        api_key=api_key,
        model=model,
        allowed_domains=allowed_domains,
        call=_call or _call_competitor_search,
        label="competitor-collect",
    )


def _claim_ref(candidate: WebFactCandidate) -> str:
    """Deterministic per-fact id so re-analysis is idempotent (the claims unique
    index is org+data_source_id+claim_ref).

    Keyed on the fact's IDENTITY (source URL + metric/assertion class + subject),
    NOT its free-text wording: the model rephrases the same fact run-to-run, so
    including value.raw would mint a fresh claim every re-analysis and let web
    claims accumulate unboundedly. Keying on identity makes the same
    (URL, metric, subject) collapse via the unique index (ON CONFLICT DO NOTHING)
    on the next run instead. The trade-off -- two genuinely distinct assertions
    that share a URL, class, and subject collapse to one -- is acceptable for
    best-effort enrichment and far better than unbounded growth."""
    basis = "\x1f".join(
        [
            candidate.source_url,
            candidate.attribute_raw or candidate.assertion_class or "",
            candidate.entity,
        ]
    )
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
    return f"web:{digest}"


async def persist_web_facts(
    db: AsyncSession, *, deal_id: Any, org_id: int, candidates: Sequence[WebFactCandidate]
) -> int:
    """Mint candidates as `web` claims under synthetic per-URL data_source rows.
    Idempotent: one data_source per (deal, source_url), claims upserted on the
    org+data_source_id+claim_ref unique index (ON CONFLICT DO NOTHING), so a
    re-analysis does not duplicate. Returns the number of claim rows inserted.
    `db` must already be RLS-scoped by the caller."""
    if not candidates:
        return 0

    # One web data_source per distinct source URL for this deal (get-or-create).
    existing = {
        ds.source_url: ds.id
        for ds in await _list_web_data_sources(db, deal_id)
        if ds.source_url is not None
    }
    source_ids: dict[str, Any] = dict(existing)
    for candidate in candidates:
        if candidate.source_url in source_ids:
            continue
        ds = DataSource(
            org_id=org_id,
            deal_id=deal_id,
            storage_key=f"web/{_claim_ref(candidate)}",
            filename=candidate.source_title,
            source_url=candidate.source_url,
            declared_sha256=hashlib.sha256(candidate.source_url.encode("utf-8")).hexdigest(),
        )
        db.add(ds)
        await db.flush()
        source_ids[candidate.source_url] = ds.id

    rows = [
        {
            "org_id": org_id,
            "deal_id": deal_id,
            "data_source_id": source_ids[c.source_url],
            "claim_ref": _claim_ref(c),
            # entity is a required column and is guaranteed non-empty here:
            # _adjudicate drops any qualitative candidate with an empty subject
            # and every sizing candidate carries a market-descriptor entity.
            "entity": c.entity,
            "attribute": c.attribute,
            "attribute_raw": c.attribute_raw,
            "value": c.value,
            "kind": "web",
            "status": "cited",
            "verification_method": "direct_read",
            "claim_kind": c.claim_kind,
            "assertion_class": c.assertion_class,
            "claim_type": "entity_attribute" if c.claim_kind == "qualitative" else "numerical",
        }
        for c in candidates
    ]
    stmt = (
        pg_insert(Claim)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["org_id", "data_source_id", "claim_ref"])
        .returning(Claim.id)
    )
    result = await db.execute(stmt)
    return len(result.all())


async def _list_web_data_sources(db: AsyncSession, deal_id: Any) -> list[DataSource]:
    """The deal's existing web data_source rows (those carrying a source_url), so
    persist reuses one row per URL across re-analysis instead of piling up
    duplicate sources."""
    result = await db.execute(
        select(DataSource).where(DataSource.deal_id == deal_id, DataSource.source_url.is_not(None))
    )
    return list(result.scalars().all())
