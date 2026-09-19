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
import re
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
# Leadership (people) section caps. Background reuses _MAX_POINT_CHARS.
_MAX_PEOPLE = 12
_MAX_NAME_CHARS = 120
_MAX_TITLE_CHARS = 160
# Sparse-only until embeddings are backfilled (every chunk's embedding is NULL
# today, so the dense leg matches nothing regardless). Retrieval is built so
# flipping this to include a dense weight + a query vector is the only change.
_WEIGHTS = RRFWeights(dense=0.0, sparse=1.0)
# Hard ceiling per section call so a slow request fails fast (-> that section
# skipped) instead of stalling the whole pass.
_LLM_TIMEOUT_S = 45.0


@dataclass(frozen=True)
class SectionSpec:
    """One narrative section to synthesize for a deal. `key` identifies the section
    to the tab that renders it -- the Company-tab prose sections reuse
    build_company_view's names (overview/risks/commercial/related_parties/plans),
    while executive_summary (Summary tab), leadership (Founders), and the market_*
    sections (Market tab) have no build_company_view counterpart. `query` is the
    keyword-rich retrieval query."""

    key: str
    title: str
    question: str
    query: str
    people: bool = False


# The narrative sections synthesized per deal, one snapshot shared across tabs.
# overview/risks/commercial/related_parties feed the Company tab and "leadership"
# its people section; "executive_summary" is deal-level and feeds the Summary
# tab's Executive Summary (the memo composer that used to write it is unbuilt);
# "market_risks"/"market_growth_strategy" feed the Market tab, whose claims spine
# has no producer for a market-scoped risk or growth signal. Identity facts
# (Sector / HQ / Headcount / Founded) are deliberately NOT here -- they stay on
# the claims + deal-profile path; this pass is for the prose sections where
# synthesis over the full text adds the most over the atomic claim dump. All
# sections are served by GET /deals/{id}/company-synthesis and cached under one
# query key, so the tabs share a single synthesis pass; each tab renders only the
# sections it needs by key.
COMPANY_SECTIONS: tuple[SectionSpec, ...] = (
    SectionSpec(
        "executive_summary",
        "Executive Summary",
        (
            "Summarize this company as a potential investment: what it does and how "
            "it makes money, the market it serves, its commercial traction, and its "
            "principal risks."
        ),
        (
            "business model products services market size customers revenue growth "
            "traction competitive position risks investment thesis"
        ),
    ),
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
    # --- Market tab -----------------------------------------------------------
    # These two feed the MARKET tab, and are deliberately a DIFFERENT lens from the
    # Company tab's `risks` / `plans` above -- not a relabel of them. Those stay the
    # OPERATIONAL lens (the target's own key-person, supply-chain, execution risks
    # and general commitments), lead-subject-scoped through build_company_view; the
    # claims spine already carries them as risk_or_dependency / plan_or_commitment.
    # These are the EXTERNAL, market-facing lens the claims spine has no producer
    # for: risk arising from the market/competition/regulation, and the strategy for
    # growing market position. The questions steer retrieval-synthesis toward that
    # boundary -- some overlap is inherent when both read the same filing, but the
    # framing keeps Market a market view rather than a duplicate of Company under a
    # "market" heading.
    SectionSpec(
        "market_risks",
        "Market Risks",
        (
            "What risks does the company face from its MARKET environment -- "
            "competition and competitive threats, market-size or growth assumptions, "
            "pricing and margin pressure, regulatory or policy change, industry "
            "cyclicality, and demand or substitution risk? Report market-, "
            "competitive- and regulatory-facing risks, not the company's purely "
            "internal operational risks (key-person, supply-chain execution, "
            "litigation)."
        ),
        (
            "market risk competition competitive threats new entrants market share "
            "erosion pricing pressure margin regulatory policy change compliance "
            "industry cyclicality demand substitution addressable market growth "
            "assumptions macroeconomic headwinds"
        ),
    ),
    SectionSpec(
        "market_growth_strategy",
        "Growth Strategy",
        (
            "How does the company plan to GROW its position in the market -- its "
            "market-expansion and go-to-market strategy, new segments, geographies "
            "or products, how it intends to win share and differentiate from "
            "competitors, and its stated growth drivers?"
        ),
        (
            "growth strategy go to market market expansion new markets geographies "
            "segments product roadmap customer acquisition win market share "
            "competitive differentiation upsell cross sell partnerships channel "
            "penetration total addressable market"
        ),
    ),
    SectionSpec(
        "leadership",
        "Leadership",
        "Who are the company's founders, executives, and directors -- their names, "
        "roles, and stated background?",
        "founders chief executive officer chairman managing director management team "
        "executives directors appointed biography background prior experience career role",
        people=True,
    ),
)


@dataclass(frozen=True)
class SynthCitation:
    """One (document, page) a grounded point is verified against. `page` may be
    None for a page-less chunk (a table/chart); the point still survived."""

    document_id: str
    page: int | None

    def to_json(self) -> dict[str, Any]:
        return {"document_id": self.document_id, "page": self.page}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SynthCitation":
        page = data.get("page")
        return cls(document_id=str(data.get("document_id", "")), page=page)


@dataclass(frozen=True)
class SynthPoint:
    """One grounded point: a short sentence, the (document, page) citations it is
    verified against (deduped, for display), and the real chunk UUIDs behind it
    (for provenance/traceability)."""

    text: str
    citations: list[SynthCitation]
    chunk_ids: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "citations": [c.to_json() for c in self.citations],
            "chunk_ids": list(self.chunk_ids),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SynthPoint":
        raw_cites = data.get("citations")
        citations = [
            SynthCitation.from_json(c)
            for c in (raw_cites if isinstance(raw_cites, list) else [])
            if isinstance(c, dict)
        ]
        raw_chunks = data.get("chunk_ids")
        chunk_ids = [str(c) for c in raw_chunks] if isinstance(raw_chunks, list) else []
        return cls(text=str(data.get("text", "")), citations=citations, chunk_ids=chunk_ids)


@dataclass(frozen=True)
class SynthPerson:
    """One grounded leadership entry: name/title/background plus the same
    (document, page) citation + chunk-id provenance pattern as SynthPoint."""

    name: str
    title: str | None
    background: str | None
    citations: list[SynthCitation]
    chunk_ids: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "background": self.background,
            "citations": [c.to_json() for c in self.citations],
            "chunk_ids": list(self.chunk_ids),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SynthPerson":
        raw_cites = data.get("citations")
        citations = [
            SynthCitation.from_json(c)
            for c in (raw_cites if isinstance(raw_cites, list) else [])
            if isinstance(c, dict)
        ]
        raw_chunks = data.get("chunk_ids")
        chunk_ids = [str(c) for c in raw_chunks] if isinstance(raw_chunks, list) else []
        title = data.get("title")
        background = data.get("background")
        return cls(
            name=str(data.get("name", "")),
            title=title if isinstance(title, str) else None,
            background=background if isinstance(background, str) else None,
            citations=citations,
            chunk_ids=chunk_ids,
        )


@dataclass(frozen=True)
class SectionSynthesis:
    """A section carries `points` (the prose sections) or `people` (the
    leadership section) -- never conceptually both -- but both fields always
    exist so an old persisted row with no `people` key still degrades
    gracefully to an empty list rather than raising."""

    key: str
    title: str
    points: list[SynthPoint] = field(default_factory=list)
    people: list[SynthPerson] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "points": [p.to_json() for p in self.points],
            "people": [p.to_json() for p in self.people],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SectionSynthesis":
        raw_points = data.get("points")
        points = [
            SynthPoint.from_json(p)
            for p in (raw_points if isinstance(raw_points, list) else [])
            if isinstance(p, dict)
        ]
        raw_people = data.get("people")
        people = [
            SynthPerson.from_json(p)
            for p in (raw_people if isinstance(raw_people, list) else [])
            if isinstance(p, dict)
        ]
        return cls(
            key=str(data.get("key", "")),
            title=str(data.get("title", "")),
            points=points,
            people=people,
        )


def sections_to_json(sections: Sequence[SectionSynthesis]) -> list[dict[str, Any]]:
    """Serialize a synthesis result to the JSONB shape stored in synthesis_snapshot."""
    return [s.to_json() for s in sections]


def sections_from_json(data: Any) -> list[SectionSynthesis]:
    """Deserialize the synthesis_snapshot JSONB back into SectionSynthesis objects.
    Tolerant of a malformed row (returns what it can) -- a corrupt snapshot must
    fail soft to the claims-driven fallback, never 500 the page."""
    if not isinstance(data, list):
        return []
    return [SectionSynthesis.from_json(s) for s in data if isinstance(s, dict)]


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


_PEOPLE_SYSTEM = (
    "You are a private-equity diligence analyst. You are given numbered excerpts "
    "from a target company's OWN documents and asked to identify its leadership.\n\n"
    "Hard rules:\n"
    "- Use ONLY the excerpts provided. Extract only people actually named in them; "
    "never infer, guess, or introduce a person, title, or background fact not "
    "stated in the excerpts.\n"
    "- Ground every person in specific excerpts and cite their [c#] id(s). Never "
    "cite an id that is not in the list, and never report a person you cannot cite.\n"
    "- `title` is the person's stated role (e.g. Chief Executive Officer, Chairman, "
    "Director); leave it null if no title is stated.\n"
    "- `background` is a short, concrete summary of their stated prior experience "
    "or biography; leave it null if the excerpts state none.\n"
    "- One entry per person -- merge duplicate mentions of the same person into a "
    "single entry citing all the excerpts that mention them.\n"
    "- Report people at the TARGET company only, not an advisor, investor, or "
    "customer mentioned in passing.\n"
    f"- Report at most {_MAX_PEOPLE} people. If more are named, choose the "
    f"{_MAX_PEOPLE} most senior (by title) or most clearly described -- never "
    "truncate an entry mid-way to fit more people in.\n"
    "- If the excerpts do not name any people, set found=false and return no people."
)

_PEOPLE_TOOL: "ToolParam" = {
    "name": "report_people",
    "description": (
        "Report the grounded people (founders, executives, directors), each citing "
        f"its excerpt ids. At most {_MAX_PEOPLE} people -- if more are named, report "
        "only the most senior/clearly described ones."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "found": {
                "type": "boolean",
                "description": (
                    "true if the excerpts name people at the company; false otherwise."
                ),
            },
            "people": {
                "type": "array",
                "description": (
                    f"Grounded people, at most {_MAX_PEOPLE}; empty when found is false."
                ),
                "maxItems": _MAX_PEOPLE,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "The person's full name, as stated in the excerpts.",
                        },
                        "title": {
                            "type": ["string", "null"],
                            "description": "Their stated title/role, or null if not stated.",
                        },
                        "background": {
                            "type": ["string", "null"],
                            "description": (
                                "A short summary of their stated prior experience/biography, "
                                "or null if not stated."
                            ),
                        },
                        "chunk_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "The [c#] excerpt id(s) this person is grounded in; "
                                "never invent one."
                            ),
                        },
                    },
                    "required": ["name", "chunk_ids"],
                },
            },
        },
        "required": ["found", "people"],
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
    *,
    api_key: str,
    model: str,
    question: str,
    company: str,
    hits: list[ChunkHit],
    system: str,
    tool: "ToolParam",
    max_tokens: int,
) -> Any:
    """Blocking Anthropic call -- forced tool use gives a structured result
    without relying on the model to format JSON in free text. Run via
    asyncio.to_thread so it never blocks the event loop. Returns the tool input
    dict (e.g. {found, points} or {found, people}) or None if the model did not
    call the tool. `system`/`tool`/`max_tokens` are passed explicitly since the
    people (leadership) call uses a different prompt, tool, and token budget
    than the points call."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=_LLM_TIMEOUT_S)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[
            {
                "role": "user",
                "content": _build_user_message(question=question, company=company, hits=hits),
            }
        ],
        # temperature=0 for reproducibility: this synthesis runs at request time on
        # every page load, so a non-zero temperature makes the Business Overview /
        # Risks / Commercial / Executive Summary text drift between two loads of the
        # same deal. The Messages API has no seed. This SDK build exposes no
        # `temperature` kwarg, so it goes through extra_body -- the documented escape
        # hatch that merges straight into the request body.
        extra_body={"temperature": 0},
    )
    for block in message.content:
        # getattr throughout: message.content is a union of block types and only
        # the tool_use arm carries name/input -- pyright cannot narrow on the
        # runtime `type` check, so read defensively (mirrors screening_insights).
        if getattr(block, "type", None) != "tool_use":
            continue
        if getattr(block, "name", None) != tool["name"]:
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


def _verify_people(raw: Any, hits: Sequence[ChunkHit]) -> list[SynthPerson]:
    """Deterministic grounding gate for the leadership section, structured like
    _verify_points with one extra gate: a person survives only if (1) they cite a
    real retrieved excerpt id AND (2) their surname actually appears in the text
    of at least one of those resolved chunks -- a person whose citation is
    structurally valid but whose name the model invented (or attached to the
    wrong excerpt) is still dropped."""
    if not isinstance(raw, dict) or not raw.get("found"):
        return []
    people = raw.get("people")
    if not isinstance(people, list):
        return []
    by_id = {_excerpt_id(i): hit for i, hit in enumerate(hits)}
    out: list[SynthPerson] = []
    seen: set[str] = set()
    for person in people:
        if not isinstance(person, dict):
            continue
        name = person.get("name")
        cited = person.get("chunk_ids")
        if not isinstance(name, str) or not isinstance(cited, list):
            continue
        name = " ".join(name.split()).strip()
        if not name or len(name) > _MAX_NAME_CHARS:
            continue
        title = person.get("title")
        title = " ".join(title.split()).strip() if isinstance(title, str) else ""
        if len(title) > _MAX_TITLE_CHARS:
            continue
        background = person.get("background")
        background = " ".join(background.split()).strip() if isinstance(background, str) else ""
        if len(background) > _MAX_POINT_CHARS:
            continue
        matched = [by_id[c] for c in cited if isinstance(c, str) and c in by_id]
        if not matched:
            continue  # every cited id was invented -> ungrounded -> drop
        # Word-boundary match (not raw substring) so this applies uniformly
        # regardless of surname length -- a short surname like "Li" or "Wu"
        # would otherwise skip the gate entirely (raw substring would also
        # false-positive inside an unrelated word like "liability"; \b avoids
        # both problems without needing a length cutoff).
        surname = name.split()[-1].casefold()
        surname_re = re.compile(r"\b" + re.escape(surname) + r"\b")
        if not any(surname_re.search(h.content.casefold()) for h in matched):
            continue  # citation resolves, but the name isn't actually in the text -> drop
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        citations: list[SynthCitation] = []
        cite_seen: set[tuple[str, int | None]] = set()
        for h in matched:
            ck = (str(h.document_id), h.page)
            if ck in cite_seen:
                continue
            cite_seen.add(ck)
            citations.append(SynthCitation(document_id=str(h.document_id), page=h.page))
        chunk_ids = [str(h.chunk_id) for h in matched]
        out.append(
            SynthPerson(
                name=name,
                title=title or None,
                background=background or None,
                citations=citations,
                chunk_ids=chunk_ids,
            )
        )
        if len(out) >= _MAX_PEOPLE:
            break
    return out


async def retrieve(
    session: "AsyncSession",
    *,
    org_id: str,
    document_ids: Sequence[str],
) -> list[tuple[SectionSpec, list[ChunkHit]]]:
    """Phase A (DB-only): the per-section chunk retrieval for every COMPANY_SECTION.

    Kept separate from `generate` so the persist stage can hold the org-scoped
    read transaction ONLY here and run the LLM gather with no transaction open --
    running the ~45s-per-section gather inside a worker txn would pin a pooled
    PgBouncer backend and can trip idle_in_transaction_session_timeout.

    SEQUENTIAL: a single AsyncSession is not concurrency-safe, so the per-section
    DB queries cannot overlap. A section whose retrieval raises is dropped (logged)
    and simply absent from the result -- the pass fails soft per section."""
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
                # A section query is a keyword bag (e.g. "business overview products
                # services ... segments"); ANDing every term matches no single chunk
                # (verified: AND -> 0 hits, OR -> 48 on a real deal). Recall mode ORs
                # them and lets ts_rank_cd rank -- essential while retrieval is
                # sparse-only (dense embeddings not yet backfilled).
                match_mode="or",
            )
            retrieved.append((spec, hits))
        except Exception:
            logger.warning("field-synthesis retrieval failed for %s", spec.key, exc_info=True)
    return retrieved


async def generate(
    *,
    api_key: str,
    model: str,
    company: str,
    retrieved: Sequence[tuple[SectionSpec, list[ChunkHit]]],
) -> list[SectionSynthesis]:
    """Phase B (no DB): the parallel, grounded LLM pass over the retrieved chunks.
    Returns only sections that produced at least one verified point. Network-bound
    and independent per section, so run concurrently; holds no transaction."""

    # Then the LLM calls in parallel -- network-bound, and each is independent.
    # Each returns (reason_code, section-or-None); the reason distinguishes the
    # empty paths so the summary log below explains a blank section.
    async def _run(spec: SectionSpec, hits: list[ChunkHit]) -> tuple[str, SectionSynthesis | None]:
        if not hits:
            return ("no_hits", None)
        try:
            raw = await asyncio.to_thread(
                _call_model,
                api_key=api_key,
                model=model,
                question=spec.question,
                company=company,
                hits=hits,
                system=_PEOPLE_SYSTEM if spec.people else _SYSTEM,
                tool=_PEOPLE_TOOL if spec.people else _TOOL,
                # People budget sized for _MAX_PEOPLE (12) entries at up to
                # _MAX_NAME_CHARS+_MAX_TITLE_CHARS+_MAX_POINT_CHARS chars each
                # plus JSON/chunk_ids overhead -- 2048 could truncate a
                # leadership-heavy document (board/team page) mid-tool-call.
                max_tokens=4096 if spec.people else 1024,
            )
        except Exception:
            logger.warning(
                "field-synthesis LLM call failed for %r/%s", company, spec.key, exc_info=True
            )
            return ("llm_error", None)
        # The leadership section verifies/reports via `people` instead of `points`;
        # everything else about the reason-code classification below is shared.
        result_key = "people" if spec.people else "points"
        if spec.people:
            people = _verify_people(raw, hits)
            if people:
                return ("ok", SectionSynthesis(key=spec.key, title=spec.title, people=people))
        else:
            points = _verify_points(raw, hits)
            if points:
                return ("ok", SectionSynthesis(key=spec.key, title=spec.title, points=points))
        # Nothing survived. Separate the three distinct empty causes -- they point
        # at different fixes: model_no_tool_call (the model returned no structured
        # tool call at all -> prompt/model), model_no_answer (it answered found=false
        # -> retrieval/query didn't surface the section), and ungrounded (it answered
        # with points/people but the grounding gate dropped every one as citing an
        # unretrieved id, or (people only) failing the surname-presence gate ->
        # prompt/gate).
        if raw is None:
            return ("model_no_tool_call", None)
        model_answered = (
            isinstance(raw, dict)
            and bool(raw.get("found"))
            and isinstance(raw.get(result_key), list)
            and bool(raw.get(result_key))
        )
        return ("ungrounded" if model_answered else "model_no_answer", None)

    # Sections whose retrieval raised never entered `retrieved`; default them to
    # retrieval_error so the summary accounts for every COMPANY_SECTION.
    outcomes: dict[str, str] = {spec.key: "retrieval_error" for spec in COMPANY_SECTIONS}
    run_results = await asyncio.gather(*(_run(spec, hits) for spec, hits in retrieved))
    sections: list[SectionSynthesis] = []
    for (spec, _hits), (reason, section) in zip(retrieved, run_results, strict=True):
        outcomes[spec.key] = reason
        if section is not None:
            sections.append(section)

    n_ok = sum(1 for r in outcomes.values() if r == "ok")
    summary = ", ".join(f"{spec.key}={outcomes[spec.key]}" for spec in COMPANY_SECTIONS)
    logger.info(
        "field-synthesis for %r: %d/%d sections grounded (%s)",
        company,
        n_ok,
        len(COMPANY_SECTIONS),
        summary,
    )
    return sections


def snapshot_reason(
    *, has_api_key: bool, has_documents: bool, sections: Sequence[SectionSynthesis]
) -> str:
    """The deal-level sentinel persisted with a snapshot (SynthesisSnapshot.REASONS):
    why an empty snapshot is empty, so a blank page is a recorded fact rather than
    indistinguishable from a not-yet-computed one. Pure."""
    if not has_api_key:
        return "no_api_key"
    if not has_documents:
        return "no_documents"
    if not sections:
        return "no_sections_grounded"
    return "ok"


async def synthesize_company_sections(
    session: "AsyncSession",
    *,
    org_id: str,
    document_ids: Sequence[str],
    company: str,
) -> list[SectionSynthesis]:
    """Grounded AI summaries for the Company tab's narrative sections (back-compat
    convenience over retrieve + generate on one session). Returns only sections
    that produced at least one verified point. Fails soft: no API key or no
    documents yields an empty list (logged with the reason), never an exception.

    The persist stage (start_deal_synthesis) does NOT use this -- it calls
    retrieve() and generate() in separate transaction phases so the LLM gather
    never runs inside a held DB transaction."""
    settings = get_settings()
    if not settings.anthropic_api_key or not document_ids:
        logger.info(
            "field-synthesis skipped for %r: reason=%s -- the FE renders the "
            "claims-driven fallback",
            company,
            "no_api_key" if not settings.anthropic_api_key else "no_documents",
        )
        return []
    retrieved = await retrieve(session, org_id=org_id, document_ids=document_ids)
    return await generate(
        api_key=settings.anthropic_api_key,
        model=settings.field_synthesis_model,
        company=company,
        retrieved=retrieved,
    )
