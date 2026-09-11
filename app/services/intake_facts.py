"""Mint a deal's intake-questionnaire answers as `intake` claims.

The external party (founder/analyst) answers a small set of free-text diligence
questions when a deal is collected (deal_intake_response.answers). Those answers
are first-party, attested statements about the company's business, market,
competitors and commercials -- exactly the qualitative content the Company and
Market tabs surface -- but nothing consumed them into the analysis until now.

This module maps each answered question to a claim `assertion_class` by matching
its PROMPT text (question_key is admin-defined per org, so the wording is the
stable signal), then mints one `intake` claim per (question, class) under a single
synthetic per-deal intake data_source. The Company/Market view builders already
route qualitative claims by assertion_class, so a minted intake claim surfaces in
its box with a citation and trust pill, no view changes needed:

    operating_model      -> Company - Business Overview  (company.overview)
    commercial_terms     -> Company - Commercial Terms    (company.commercial)
    market_definition    -> Market  - Market Definition
    competitive_position -> Market  - Competitive Position

Trust posture: minted `cited` with verification_method `direct_read` (we read the
submitted answer bytes exactly), and DELIBERATELY excluded from the status roll-up
(see the `kind != 'intake'` guard on both roll_up_deal SELECTs) so a self-report
stays `cited` and can never be promoted to `verified` -- an unaudited founder
answer must not read as independently verified. This mirrors how `web` claims are
minted-and-left-cited (app/services/web_search_collect.py).

No LLM: the mapping is deterministic keyword matching, so this path is unaffected
by the Anthropic usage limit that gates web-collect and prose extraction.

`db` must already be RLS-scoped by the caller (SET LOCAL app.org_id), same
contract as the rest of app/services/.
"""

import hashlib
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.claim import Claim
from app.models.data_source import DataSource

# The citation label shown on every intake-sourced fact (data_source.filename ->
# _citation). Deliberately generic and honest about provenance.
INTAKE_SOURCE_LABEL = "Founder intake questionnaire"

# Prompt-keyword -> assertion_class, checked in order; first match wins for a
# given question. Keyed on the prompt WORDING (not question_key, which is
# admin-defined and unstable) and scoped tightly enough that the seven standard
# questions each land on exactly one class. A prompt matching no rule is skipped
# rather than guessed -- an unmapped answer stays out of the tabs, never mis-filed.
_CLASS_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # target market segment / (estimated) addressable market size
    (
        re.compile(
            r"addressable market|market segment|target market|\btam\b|\bsam\b|\bsom\b", re.I
        ),
        "market_definition",
    ),
    # direct/indirect competitors + differentiators
    (re.compile(r"competitor|competitive|differentiat", re.I), "competitive_position"),
    # monetization / pricing model
    (
        re.compile(r"monetiz|monetis|pricing|subscription|licensing|revenue model", re.I),
        "commercial_terms",
    ),
    # commercial traction: ARR / run rate / pipeline
    (
        re.compile(r"traction|recurring revenue|\barr\b|run[\s-]?rate|pipeline", re.I),
        "commercial_terms",
    ),
    # target customers / design partners / pilots
    (re.compile(r"customer|design partner|deployment partner|\bpilot", re.I), "commercial_terms"),
    # value proposition / product architecture
    (re.compile(r"value proposition|core value|architecture|platform", re.I), "operating_model"),
    # what problem are you solving
    (re.compile(r"problem", re.I), "operating_model"),
)

# assertion_class -> a short human label carried on attribute_raw, so a fact reads
# with its own header even when several share the same section (e.g. three
# commercial_terms rows). Purely cosmetic; the answer text is the substance.
_LABEL_BY_CLASS = {
    "operating_model": "Business overview",
    "commercial_terms": "Commercial",
    "market_definition": "Market definition",
    "competitive_position": "Competitive position",
}


def _classify(prompt: str) -> str | None:
    for pattern, assertion_class in _CLASS_RULES:
        if pattern.search(prompt):
            return assertion_class
    return None


@dataclass(frozen=True)
class IntakeFactCandidate:
    """One adjudicated intake answer in claim shape. `question_key` keys the
    deterministic claim_ref so re-analysis is idempotent; `entity` is the target
    company (company_view subject-filters qualitative claims to the lead subject);
    `assertion_class` routes it to its Company/Market section."""

    question_key: str
    assertion_class: str
    attribute_raw: str | None
    entity: str
    text: str


def build_intake_candidates(answers: Any, *, company: str) -> list[IntakeFactCandidate]:
    """Turn a deal_intake_response.answers blob into claim-shaped candidates.

    The stored blob is `{"schema_version": 1, "answers": [{question_key, prompt,
    answer, answered}, ...]}` (a bare list is also tolerated). Only questions the
    party actually answered with non-whitespace text are minted; each is mapped to
    an assertion_class by its prompt wording, and an unmappable prompt is skipped.
    Pure: no DB, no network."""
    entries = answers.get("answers") if isinstance(answers, dict) else answers
    if not isinstance(entries, list):
        return []

    # entity must be non-empty (claims.entity is NOT NULL) and should read as the
    # target so company_view's lead-subject filter keeps it.
    entity = (company or "").strip() or "The company"

    seen_keys: set[str] = set()
    candidates: list[IntakeFactCandidate] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if not entry.get("answered"):
            continue
        text = (entry.get("answer") or "").strip()
        prompt = (entry.get("prompt") or "").strip()
        if not text or not prompt:
            continue
        assertion_class = _classify(prompt)
        if assertion_class is None:
            continue
        # question_key anchors the claim_ref; fall back to the prompt when a blob
        # somehow omits it, so a keyless entry is still idempotent run-to-run.
        question_key = str(entry.get("question_key") or prompt)
        if question_key in seen_keys:
            continue
        seen_keys.add(question_key)
        candidates.append(
            IntakeFactCandidate(
                question_key=question_key,
                assertion_class=assertion_class,
                attribute_raw=_LABEL_BY_CLASS.get(assertion_class),
                entity=entity,
                text=text,
            )
        )
    return candidates


def _claim_ref(candidate: IntakeFactCandidate) -> str:
    """Deterministic per-answer id so re-analysis is idempotent (the claims unique
    index is org+data_source_id+claim_ref). Keyed on the question's IDENTITY
    (question_key + assertion_class), NOT the answer text: an edited/re-submitted
    answer to the same question updates in place rather than accumulating a new
    row every re-analysis."""
    basis = f"{candidate.question_key}\x1f{candidate.assertion_class}"
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
    return f"intake:{digest}"


async def _get_or_create_intake_source(
    db: AsyncSession, *, deal_id: Any, org_id: int, intake_link_id: uuid.UUID | None
) -> uuid.UUID:
    """The deal's single synthetic intake data_source (one per deal), reused across
    re-analysis rather than piling up duplicates."""
    storage_key = f"intake/{deal_id}"
    existing = (
        await db.scalars(
            select(DataSource.id).where(
                DataSource.deal_id == deal_id, DataSource.storage_key == storage_key
            )
        )
    ).first()
    if existing is not None:
        return existing
    ds = DataSource(
        org_id=org_id,
        deal_id=deal_id,
        storage_key=storage_key,
        filename=INTAKE_SOURCE_LABEL,
        source_url=None,
        declared_sha256=hashlib.sha256(storage_key.encode("utf-8")).hexdigest(),
        intake_link_id=intake_link_id,
    )
    db.add(ds)
    await db.flush()
    return ds.id


async def persist_intake_facts(
    db: AsyncSession,
    *,
    deal_id: Any,
    org_id: int,
    intake_link_id: uuid.UUID | None,
    candidates: Sequence[IntakeFactCandidate],
) -> int:
    """Mint candidates as `intake` claims under the deal's single synthetic intake
    data_source. Idempotent: claims upserted on the org+data_source_id+claim_ref
    unique index (ON CONFLICT DO NOTHING), so a re-analysis does not duplicate.
    Returns the number of claim rows inserted. `db` must already be RLS-scoped."""
    if not candidates:
        return 0

    source_id = await _get_or_create_intake_source(
        db, deal_id=deal_id, org_id=org_id, intake_link_id=intake_link_id
    )

    rows = [
        {
            "org_id": org_id,
            "deal_id": deal_id,
            "data_source_id": source_id,
            "claim_ref": _claim_ref(c),
            "entity": c.entity,
            "attribute": "operating_metric",
            "attribute_raw": c.attribute_raw,
            "value": {"raw": c.text, "normalized": None, "unit": None, "value_type": "text"},
            "kind": "intake",
            "status": "cited",
            "verification_method": "direct_read",
            "claim_kind": "qualitative",
            "assertion_class": c.assertion_class,
            "claim_type": "entity_attribute",
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
