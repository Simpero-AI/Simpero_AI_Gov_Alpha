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
# (out per the provider decision) and anything user-generated. Tunable; passed to
# the web_search tool's allowed_domains AND re-checked in the adjudicator.
DEFAULT_ALLOWED_DOMAINS: tuple[str, ...] = (
    "grandviewresearch.com",
    "mordorintelligence.com",
    "marketsandmarkets.com",
    "statista.com",
    "ibisworld.com",
    "gartner.com",
    "forrester.com",
    "mckinsey.com",
    "reuters.com",
    "bloomberg.com",
    "wsj.com",
    "ft.com",
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

# section (model-facing) -> assertion_class (claims spine). Kept in lockstep with
# company_view/market_view's routing so a collected assertion lands in the right
# tab section.
_SECTION_TO_ASSERTION_CLASS: dict[str, str] = {
    "market_definition": "market_definition",
    "competitive_position": "competitive_position",
    "company_overview": "operating_model",
    "company_risks": "risk_or_dependency",
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
        "Collect:\n"
        "- Market sizing: TAM/SAM/SOM, overall market size, and market CAGR.\n"
        "- Competitive position: named competitors and how rivals are positioned.\n"
        "- Market definition: what the market is, its structure and growth drivers.\n"
        "- Company overview: what the company does and how it operates.\n"
        "- Risks, related parties, and stated plans, when publicly reported.\n\n"
        "Hard rules:\n"
        "- Report ONLY facts that appear in a search result, each with the exact "
        "source URL it came from. Never estimate or invent a figure, name, or URL.\n"
        "- Prefer authoritative sources (market-research firms, regulators, major press).\n"
        "- If you find nothing citable for a category, omit it. Return empty "
        "arrays rather than padding."
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
                    "normalized": float(number),
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


def _call_web_search(
    *, api_key: str, model: str, company: str, sector: str | None, allowed: tuple[str, ...]
) -> dict[str, Any]:
    """Blocking Anthropic call with the web_search server tool + the report tool.
    The model searches (server-side, bounded by max_uses + allowed_domains) then
    calls report_web_facts; we return that tool input. Run via asyncio.to_thread."""
    import anthropic

    # max_retries=0: this is best-effort enrichment that already fails soft to [],
    # so a single attempt is the right posture -- and it keeps the max_uses/timeout
    # cost bound honest (the SDK's default retries would multiply web searches).
    client = anthropic.Anthropic(api_key=api_key, timeout=_LLM_TIMEOUT_S, max_retries=0)
    web_search_tool: dict[str, Any] = {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": _MAX_SEARCHES,
        "allowed_domains": list(allowed),
    }
    user = f"Target company: {company}" + (f"\nSector: {sector}" if sector else "")
    message = client.messages.create(
        model=model,
        max_tokens=4096,
        system=_system_prompt(),
        # cast: the pinned SDK (1.2.0) has no typed param for the web_search
        # server tool, but the API accepts the raw tool dict -- it is passed
        # through verbatim. The report tool is a normal ToolParam.
        tools=cast("Any", [web_search_tool, _report_tool()]),
        messages=[{"role": "user", "content": user}],
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
    if not api_key or not company:
        return []
    allowed = tuple(allowed_domains)
    call = _call or _call_web_search
    try:
        raw = await asyncio.to_thread(
            call, api_key=api_key, model=model, company=company, sector=sector, allowed=allowed
        )
        if not isinstance(raw, dict):
            return []
        # _adjudicate is inside the try too: an adjudication bug must also fail
        # soft to [] and never escape into the corroboration job's phase B.
        return _adjudicate(raw, frozenset(allowed))
    except Exception:
        logger.warning(
            "web-search collect failed for %r; returning no facts", company, exc_info=True
        )
        return []


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
