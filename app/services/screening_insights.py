"""Screening insights -- the LLM pass that turns a deal's extracted facts into
Agent Highlights (positive signals) and Risk Flags (concerns/gaps) for the
Initial Screening tab.

This is the only Anthropic call this backend makes itself (the parse/extract
LLM work lives in the separate parser service). It runs on demand, from the
screening-materials endpoint, over the SAME trusted claims the extracted panel
shows -- so the model reasons only about figures the user can see, and is told
in the strongest terms not to introduce any fact that is not in that list.

Fails soft on every axis: no API key -> ([], []); a model/parse/transport error
-> ([], []); no facts -> ([], []). The two panels then simply show their empty
state, and the extracted panel (which needs no LLM) is never affected.
"""

import asyncio
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from app.core.config import get_settings
from app.models.claim import Claim
from app.services.screening_materials import render_claim_facts

if TYPE_CHECKING:
    from anthropic.types import ToolParam

logger = logging.getLogger(__name__)

_MAX_ITEMS = 5
_MAX_ITEM_CHARS = 200

_SYSTEM = (
    "You are a private-equity diligence analyst preparing an initial screening. "
    "You are given the extracted, verified facts from a target company's materials. "
    "Surface the positive highlights and the risk flags a partner would want to see first.\n\n"
    "Hard rules:\n"
    "- Use ONLY the facts provided. Never introduce a number, name, date, entity, or claim "
    "that is not present in the list. Do not estimate, extrapolate, or assume.\n"
    "- Ground every item in the specific figures given -- reference the actual numbers and years.\n"
    "- Highlights are genuine positives (revenue growth, scale, margin strength, profitability, "
    "cash generation). Risk flags are genuine concerns (declines, volatility, thin or negative "
    "margins, losses, heavy capex, inconsistency across years).\n"
    "- Only include an item the facts clearly support. Prefer two to five of each; if the facts "
    "support fewer, return fewer. Never pad.\n"
    "- Each item is one short, concrete sentence. No preamble, no markdown."
)

_TOOL: "ToolParam" = {
    "name": "report_screening_insights",
    "description": "Report the grounded highlights and risk flags for the deal.",
    "input_schema": {
        "type": "object",
        "properties": {
            "highlights": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Positive signals, each one short grounded sentence.",
            },
            "risk_flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Concerns/gaps, each one short grounded sentence.",
            },
        },
        "required": ["highlights", "risk_flags"],
    },
}


def _clean(items: Any) -> list[str]:
    """Coerce the model's array into displayable strings: trim, drop empties and
    over-long entries, dedupe (case-insensitively), cap the count."""
    if not isinstance(items, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text or len(text) > _MAX_ITEM_CHARS:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= _MAX_ITEMS:
            break
    return out


def _call_model(*, api_key: str, model: str, company: str, facts: list[str]) -> tuple[Any, Any]:
    """Blocking Anthropic call -- forced tool use gives a structured result
    without depending on the model to format JSON in free text. Run via
    asyncio.to_thread so it never blocks the event loop."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    user = f"COMPANY: {company}\n\nEXTRACTED FACTS (the only facts you may use):\n" + "\n".join(
        f"- {f}" for f in facts
    )
    message = client.messages.create(
        model=model,
        max_tokens=1024,
        system=_SYSTEM,
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": _TOOL["name"]},
        messages=[{"role": "user", "content": user}],
    )
    for block in message.content:
        # getattr throughout: message.content is a union of block types and only
        # the tool_use arm carries name/input -- pyright cannot narrow on the
        # runtime `type` check, so read defensively.
        if getattr(block, "type", None) != "tool_use":
            continue
        if getattr(block, "name", None) != _TOOL["name"]:
            continue
        data = getattr(block, "input", None)
        if isinstance(data, dict):
            return data.get("highlights"), data.get("risk_flags")
    return None, None


async def derive_screening_insights(
    claims: Sequence[Claim],
    *,
    company: str,
    dashboard_structure: dict[str, Any] | None,
) -> tuple[list[str], list[str]]:
    """(highlights, risk_flags) for the deal, or ([], []) when the pass cannot or
    should not run. `claims` is the deal's full claim set; the grounding is the
    trusted canonical subset (render_claim_facts)."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        return [], []

    facts = render_claim_facts(claims, dashboard_structure=dashboard_structure)
    if not facts:
        return [], []

    try:
        highlights, risk_flags = await asyncio.to_thread(
            _call_model,
            api_key=settings.anthropic_api_key,
            model=settings.screening_insights_model,
            company=company,
            facts=facts,
        )
    except Exception:
        logger.warning(
            "screening insights failed for deal %r; returning empty", company, exc_info=True
        )
        return [], []

    return _clean(highlights), _clean(risk_flags)
