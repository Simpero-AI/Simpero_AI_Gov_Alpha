"""AE-A-RETR-4 (SIM-241): memory scoping for retrieval-powered features.

Every feature that searches across documents -- Ask Me chat, Portfolio-Fit, and
any future one -- calls `org_scoped_search` here instead of `hybrid_search`
directly. This is the single seam where the served tenant is made EXPLICIT, and
it fails closed if the session it was handed is not scoped to that exact org.

WHY THIS EXISTS ON TOP OF RLS
=============================
RLS on `chunks` faithfully filters every query to whatever `app.org_id` the
session was scoped to. That is the tenant boundary, and it is deliberately NOT
duplicated here (see retrieval.py: a second WHERE clause would only drift out of
sync with the policy). What RLS cannot catch is the session being scoped to the
WRONG org for this caller: a feature that intends to serve org A but is handed a
session scoped to org B gets B's chunks back with no error -- a cross-tenant leak
with a search box in front of it. This guard cross-checks the org the feature
declares (from its own auth context) against the org the session is actually
scoped to, and refuses to search on any mismatch, or on an unscoped session.

SIM-241 exists precisely because "RLS already covers this" is NOT sufficient
evidence: the guard is explicit, and it has its own planted cross-org test
(tests/test_memory_scope_rls.py) rather than an assumption.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import MemoryScopeError
from app.services.retrieval import RRF_K, ChunkHit, RRFWeights, hybrid_search


async def assert_session_scoped_to(session: AsyncSession, org_id: str) -> None:
    """Fail closed unless the session's `app.org_id` is exactly `org_id`.

    `org_id` is the clerk_org_id the caller is authorised to serve, taken from its
    own auth context; `app.org_id` is what SET LOCAL scoped the session to and what
    RLS reads. They MUST be the same value. An empty or unset `app.org_id` means an
    unscoped session reached retrieval -- also refused, never searched against.

    Raises MemoryScopeError before any retrieval query runs.
    """
    scoped = (await session.execute(text("SELECT current_setting('app.org_id', true)"))).scalar()
    if not scoped:
        raise MemoryScopeError(
            "retrieval attempted on an org-unscoped session (app.org_id is unset)"
        )
    if scoped != org_id:
        raise MemoryScopeError(
            f"retrieval session is scoped to org {scoped!r}, "
            f"but the caller is serving org {org_id!r}"
        )


async def org_scoped_search(
    session: AsyncSession,
    *,
    org_id: str,
    query_text: str,
    query_embedding: Sequence[float] | None = None,
    top_k: int = 10,
    weights: RRFWeights = RRFWeights(),
    document_id: str | None = None,
    document_ids: Sequence[str] | None = None,
    k: int = RRF_K,
    leg_k: int | None = None,
) -> list[ChunkHit]:
    """`hybrid_search` behind an explicit per-caller org guard.

    The single entry point every retrieval-powered feature uses; see the module
    docstring for why the guard exists on top of RLS. It fails closed (raises
    MemoryScopeError) BEFORE any query runs if `session` is not scoped to `org_id`,
    so a feature handed a wrong- or un-scoped session can never return another
    org's chunks -- independent of whether RLS alone would have caught it.

    Pass `document_ids` to scope a search to one deal's whole document set (RLS
    scopes only to the org); `query_embedding` may be omitted for a sparse-only
    search before embeddings are backfilled.
    """
    await assert_session_scoped_to(session, org_id)
    return await hybrid_search(
        session,
        query_text=query_text,
        query_embedding=query_embedding,
        top_k=top_k,
        weights=weights,
        document_id=document_id,
        document_ids=document_ids,
        k=k,
        leg_k=leg_k,
    )
