"""Grounded field synthesis -- turn a deal's OWN retrieved chunks into short,
structured, page-cited summaries for the narrative sections of the Company tab.

This augments, never replaces, the verified-claims tier. The claims spine stays
the cited/corroborated backbone; this pass reads the deal's chunks (the fuller
prose the atomic claim extraction loses) and produces a clearly-badged "AI
summary" per section. Accuracy is enforced structurally, not hoped for:

  1. Retrieve the deal's chunks for a section's question (hybrid_search, scoped to
     the deal's documents). Sparse-only today -- dense lights up once embeddings
     are backfilled (see retrieval.py); the query is keyword-rich so BM25 finds
     the right sections of a filing.
  2. Feed the model ONLY those excerpts, each labelled with a short id, and force
     it (via a tool call) to cite the excerpt id(s) behind every point.
  3. VERIFY deterministically: drop any point that cites an id we did not retrieve
     (an invented citation), then map the surviving ids to their page numbers. A
     point that survives is grounded in a real, retrieved chunk -- no ungrounded
     sentence reaches the page.

Fails soft on every axis, matching screening_insights / web_search_collect: no
API key -> {}; a model/transport error -> that section is skipped; no chunks ->
the section is simply absent. The claims-driven view is never affected.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.core.config import get_settings
from app.services.memory_scope import org_scoped_search
from app.services.retrieval import ChunkHit, RRFWeights

if TYPE_CHECKING:
    from anthropic.types import ToolParam
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Retrieval breadth per section and the per-excerpt content budget fed to the
# model. top_k wide enough to cover a section spread across a filing; the char cap
# keeps the prompt bounded on table-heavy chunks.
_TOP_K = 12
_EXCERPT_CHARS = 1500
# Display caps per section: a snapshot, not an essay.
_MAX_POINTS = 8
_MAX_POINT_CHARS = 320
# Sparse-only until embeddings are backfilled (every chunk's embedding is NULL
# today, so the dense leg matches nothing regardless). Retrieval is built so
# flipping this to include a dense weight + a query vector is the only change.
_WEIGHTS = RRFWeights(dense=0.0, sparse=1.0)
# Hard ceiling per section call so a slow request fails fast (-> that section
# skipped) instead of stalling the whole pass.
_LLM_TIMEOUT_S = 45.0


@dataclass(frozen=True)
class SectionSpec:
    """One narrative Company-tab section to synthesize. `key` matches the section
    names build_company_view already uses (overview/risks/commercial/
    related_parties/plans); `query` is the keyword-rich retrieval query."""

    key: str
    title: str
    question: str
    query: str


# The five narrative sections of the Company tab. Identity facts (Sector / HQ /
# Headcount / Founded) are deliberately NOT here -- they stay on the claims +
# deal-profile path; this pass is for the prose sections where synthesis over the
# full text adds the most over the atomic claim dump.
COMPANY_SECTIONS: tuple[SectionSpec, ...] = (
    SectionSpec(
        "overview",
        "Business Overview",
        (
            "What does the company do -- its business, its products or services, "
            "and how it generates revenue?"
        ),
        (
            "business overview products services business model how the company "
            "generates revenue operations segments"
        ),
    ),
    SectionSpec(
        "risks",
        "Risks & Dependencies",
        "What are the company's key business risks, dependencies, and uncertainties?",
        (
            "risk factors risks dependencies competition regulatory customer "
            "concentration supply chain litigation"
        ),
    ),
    SectionSpec(
        "commercial",
        "Commercial Terms",
        (
            "What are the company's commercial terms -- its customer contracts, "
            "pricing, and revenue arrangements?"
        ),
        (
            "commercial terms customer contracts pricing agreements revenue "
            "recognition backlog subscription licensing"
        ),
    ),
    SectionSpec(
        "related_parties",
        "Related Parties",
        "What related-party relationships or transactions does the company disclose?",
        (
            "related party transactions affiliates directors officers ownership "
            "interests family relationships"
        ),
    ),
    SectionSpec(
        "plans",
        "Plans & Commitments",
        "What forward-looking plans, strategy, or commitments does the company state?",
        (
            "plans strategy commitments future expansion investment outlook "
            "guidance capital allocation initiatives"
        ),
    ),
)


@dataclass(frozen=True)
class SynthCitation:
    """One (document, page) a grounded point is verified against. `page` may be
    None for a page-less chunk (a table/chart); the point still survived."""

    document_id: str
    page: int | None


@dataclass(frozen=True)
class SynthPoint:
    """One grounded point: a short sentence, the (document, page) citations it is
    verified against (deduped, for display), and the real chunk UUIDs behind it
    (for provenance/traceability)."""

    text: str
    citations: list[SynthCitation]
    chunk_ids: list[str]


@dataclass(frozen=True)
class SectionSynthesis:
    key: str
    title: str
    points: list[SynthPoint] = field(default_factory=list)


_SYSTEM = (
    "You are a private-equity diligence analyst. You are given numbered excerpts "
    "from a target company's OWN documents and one question. Answer it using ONLY "
    "those excerpts.\n\n"
    "Hard rules:\n"
    "- Use ONLY the excerpts provided. Never introduce a fact, number, name, date, "
    "or claim that is not present in them. Do not estimate, infer, or assume.\n"
    "- Ground every point in specific excerpts and cite their [c#] id(s). Never "
    "cite an id that is not in the list, and never write a point you cannot cite.\n"
    "- Report facts about the TARGET company only, not an advisor, investor, or "
    "customer mentioned in passing.\n"
    "- Each point is one short, concrete, self-contained sentence. Merge duplicates. "
    "No preamble, no markdown, no headers.\n"
    "- If the excerpts do not answer the question, set found=false and return no "
    "points. Return fewer points rather than padding."
)

_TOOL: "ToolParam" = {
    "name": "report_section",
    "description": (
        "Report the grounded points answering the question, each citing its excerpt ids."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "found": {
                "type": "boolean",
                "description": (
                    "true if the excerpts contain facts answering the question; false otherwise."
                ),
            },
            "points": {
                "type": "array",
                "description": "Grounded points; empty when found is false.",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": (
                                "One short, concrete sentence grounded in the cited excerpts."
                            ),
                        },
                        "chunk_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "The [c#] excerpt id(s) this point is grounded in; "
                                "never invent one."
                            ),
                        },
                    },
                    "required": ["text", "chunk_ids"],
                },
            },
        },
        "required": ["found", "points"],
    },
}


def _excerpt_id(index: int) -> str:
    """The short, model-facing id for the index-th retrieved chunk ("c1", "c2")."""
    return f"c{index + 1}"


def _build_user_message(*, question: str, company: str, hits: Sequence[ChunkHit]) -> str:
    lines = [
        f"COMPANY: {company}",
        f"QUESTION: {question}",
        "",
        "EXCERPTS (the only text you may use; cite these [c#] ids):",
    ]
    for i, hit in enumerate(hits):
        content = hit.content.strip().replace("\n", " ")
        if len(content) > _EXCERPT_CHARS:
            content = content[:_EXCERPT_CHARS] + "…"
        page = f"p.{hit.page}" if hit.page is not None else "n/a"
        lines.append(f"[{_excerpt_id(i)}] ({page}) {content}")
    return "\n".join(lines)


def _call_model(
    *, api_key: str, model: str, question: str, company: str, hits: list[ChunkHit]
) -> Any:
    """Blocking Anthropic call -- forced tool use gives a structured result
    without relying on the model to format JSON in free text. Run via
    asyncio.to_thread so it never blocks the event loop. Returns the tool input
    dict ({found, points}) or None if the model did not call the tool."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=_LLM_TIMEOUT_S)
    message = client.messages.create(
        model=model,
        max_tokens=1024,
        system=_SYSTEM,
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": _TOOL["name"]},
        messages=[
            {
                "role": "user",
                "content": _build_user_message(question=question, company=company, hits=hits),
            }
        ],
    )
    for block in message.content:
        # getattr throughout: message.content is a union of block types and only
        # the tool_use arm carries name/input -- pyright cannot narrow on the
        # runtime `type` check, so read defensively (mirrors screening_insights).
        if getattr(block, "type", None) != "tool_use":
            continue
        if getattr(block, "name", None) != _TOOL["name"]:
            continue
        data = getattr(block, "input", None)
        if isinstance(data, dict):
            return data
    return None


def _verify_points(raw: Any, hits: Sequence[ChunkHit]) -> list[SynthPoint]:
    """Deterministic grounding gate over the model's output. Pure (no I/O): keeps
    only points that cite at least one EXCERPT id we actually retrieved, maps those
    ids to their real chunk UUIDs + page numbers, trims/dedupes/caps. A point that
    cites only invented ids is dropped entirely -- this is what stops an ungrounded
    sentence from reaching the page."""
    if not isinstance(raw, dict) or not raw.get("found"):
        return []
    points = raw.get("points")
    if not isinstance(points, list):
        return []
    by_id = {_excerpt_id(i): hit for i, hit in enumerate(hits)}
    out: list[SynthPoint] = []
    seen: set[str] = set()
    for point in points:
        if not isinstance(point, dict):
            continue
        text = point.get("text")
        cited = point.get("chunk_ids")
        if not isinstance(text, str) or not isinstance(cited, list):
            continue
        text = " ".join(text.split()).strip()
        if not text or len(text) > _MAX_POINT_CHARS:
            continue
        matched = [by_id[c] for c in cited if isinstance(c, str) and c in by_id]
        if not matched:
            continue  # every cited id was invented -> ungrounded -> drop
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        # Display citations deduped by (document, page); chunk_ids kept in full for
        # provenance. Preserve retrieval (rank) order rather than sorting.
        citations: list[SynthCitation] = []
        cite_seen: set[tuple[str, int | None]] = set()
        for h in matched:
            ck = (str(h.document_id), h.page)
            if ck in cite_seen:
                continue
            cite_seen.add(ck)
            citations.append(SynthCitation(document_id=str(h.document_id), page=h.page))
        chunk_ids = [str(h.chunk_id) for h in matched]
        out.append(SynthPoint(text=text, citations=citations, chunk_ids=chunk_ids))
        if len(out) >= _MAX_POINTS:
            break
    return out


async def synthesize_company_sections(
    session: "AsyncSession",
    *,
    org_id: str,
    document_ids: Sequence[str],
    company: str,
) -> list[SectionSynthesis]:
    """Grounded AI summaries for the Company tab's narrative sections. Returns only
    sections that produced at least one verified point; a section with no chunks or
    no grounded answer is simply absent. Fails soft: no API key or any error yields
    an empty list, never an exception to the caller."""
    settings = get_settings()
    if not settings.anthropic_api_key or not document_ids:
        return []

    api_key = settings.anthropic_api_key
    model = settings.field_synthesis_model

    # Retrieval first, SEQUENTIALLY -- a single AsyncSession is not concurrency
    # safe, so the per-section DB queries cannot overlap.
    retrieved: list[tuple[SectionSpec, list[ChunkHit]]] = []
    for spec in COMPANY_SECTIONS:
        try:
            hits = await org_scoped_search(
                session,
                org_id=org_id,
                query_text=spec.query,
                document_ids=document_ids,
                weights=_WEIGHTS,
                top_k=_TOP_K,
            )
            retrieved.append((spec, hits))
        except Exception:
            logger.warning(
                "field-synthesis retrieval failed for %r/%s", company, spec.key, exc_info=True
            )

    # Then the LLM calls in parallel -- network-bound, and each is independent.
    async def _run(spec: SectionSpec, hits: list[ChunkHit]) -> SectionSynthesis | None:
        if not hits:
            return None
        try:
            raw = await asyncio.to_thread(
                _call_model,
                api_key=api_key,
                model=model,
                question=spec.question,
                company=company,
                hits=hits,
            )
        except Exception:
            logger.warning(
                "field-synthesis LLM call failed for %r/%s", company, spec.key, exc_info=True
            )
            return None
        points = _verify_points(raw, hits)
        if not points:
            return None
        return SectionSynthesis(key=spec.key, title=spec.title, points=points)

    results = await asyncio.gather(*(_run(spec, hits) for spec, hits in retrieved))
    return [r for r in results if r is not None]
