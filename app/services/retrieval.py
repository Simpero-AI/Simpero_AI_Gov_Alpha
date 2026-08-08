"""AE-A-RETR-3 hybrid retrieval: a dense (embedding) leg and a sparse (keyword)
leg, merged by Reciprocal Rank Fusion, always scoped to the caller's own org.

CHUNKS TABLE CONTRACT (depends on FS-A-RETR-1 / SIM-237)
=======================================================
This queries the `chunks` table by the columns AE-A-RETR-1/2 populate:
    id, org_id, document_id, content, embedding (vector), content_tsv (tsvector),
    page, char_start, char_end, element_type.
The table, its RLS policy, and its HNSW/GIN indexes are owned by SIM-237; this
module only reads. It targets that schema and does not create or migrate it.

TENANT ISOLATION (non-negotiable)
=================================
Every query runs on the AsyncSession handed in, which MUST already be org-scoped
the way get_db scopes it: `SELECT set_config('app.org_id', ..., true)` as the
first statement of the transaction. RLS on `chunks` then filters every leg to the
caller's org. This module NEVER opens its own connection and NEVER bypasses RLS --
a retrieval path with an elevated connection is a cross-tenant leak with a search
box in front of it. document_id narrows further to one document when the caller is
scoped to one (AE-A-RETR-2: >95% document-mismatch without it on a multi-doc pool).

WHY THE RRF WEIGHTING IS A CALLER PARAMETER, NOT A CONSTANT
==========================================================
The consumers have structurally different query shapes and the sparse/dense
balance that is right for one is wrong for the other:
  - Verify builds a literal, table-shaped query from a claim's own fields
    ("Acme Corp revenue $15,295"). BM25 scored 0.576 P@1 on tables -- sparse-weighted.
  - Ask Me takes a natural-language question whose words often miss the document's
    ("profitability" vs "margin"). BM25 collapses to 0.129 P@1 -- dense-weighted.
A layer tuned once for Ask Me and reused for Verify is tuned for the wrong
distribution, and nothing surfaces it because each consumer only sees its own
results. So the weight is injected per call. Per-consumer query CONSTRUCTION is a
separate ticket (AE-A-RETR-12); this only guarantees the weight has somewhere to land.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# The standard RRF smoothing constant. Large enough that a chunk ranked first in
# one leg does not automatically dominate a chunk ranked second in both; small
# enough that rank order still matters. Injectable per call for the same reason
# the weights are, but 60 is the published default and the sane starting point.
RRF_K = 60


@dataclass(frozen=True)
class RRFWeights:
    """Per-call sparse/dense balance. NOT a module constant -- see the module
    docstring. Defaults are equal; a Verify caller passes sparse-heavy, an Ask Me
    caller dense-heavy. A leg weighted 0 is dropped entirely from the fusion."""

    dense: float = 1.0
    sparse: float = 1.0


@dataclass(frozen=True)
class ChunkHit:
    """One retrieved chunk with its fused relevance score, in ranked order."""

    chunk_id: int
    content: str
    document_id: str
    page: int
    char_start: int | None
    char_end: int | None
    element_type: str
    score: float


def reciprocal_rank_fusion(
    legs: dict[str, list[int]],
    weights: dict[str, float],
    *,
    k: int = RRF_K,
) -> list[tuple[int, float]]:
    """Fuse several ranked lists of chunk ids into one ranking.

    score(chunk) = sum over legs of  weight[leg] / (k + rank_in_leg), where rank is
    1-based. A chunk surfaced by both legs outscores one surfaced by a single leg
    at the same rank, which is the whole point of fusing rather than concatenating.

    Pure and deterministic: ties are broken by chunk id so the output order is
    stable across runs. A leg with weight 0 contributes nothing (it is not merely
    down-weighted -- it is absent), so a caller can turn a leg off by zeroing it.
    Returns (chunk_id, score) pairs, best first.
    """
    scores: dict[int, float] = {}
    for leg, ranked in legs.items():
        weight = weights.get(leg, 0.0)
        if weight == 0.0:
            continue
        for rank, chunk_id in enumerate(ranked, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def _vector_literal(embedding: Sequence[float]) -> str:
    """A pgvector text literal ("[0.1,0.2,...]") for the query embedding.

    Cast `::vector` in SQL, this needs no registered asyncpg codec to be correct
    -- Postgres parses the literal. register_pgvector_codec below is the faster
    path for large vectors; this keeps the query itself codec-independent so the
    dense leg works before that wiring lands.
    """
    if not embedding:
        raise ValueError("query_embedding is empty; the dense leg has nothing to search on")
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


async def hybrid_search(
    session: AsyncSession,
    *,
    query_text: str,
    query_embedding: Sequence[float],
    top_k: int = 10,
    weights: RRFWeights = RRFWeights(),
    document_id: str | None = None,
    k: int = RRF_K,
    leg_k: int | None = None,
) -> list[ChunkHit]:
    """Run the dense and sparse legs, fuse by RRF, and return the top_k chunks.

    `session` MUST already be org-scoped (see the module docstring); this call adds
    no tenant filter of its own beyond the optional `document_id`, because RLS is
    the tenant boundary and duplicating it here would invite it to drift. Each leg
    fetches `leg_k` candidates (wider than top_k, so a chunk strong in one leg but
    absent from the other still has room to be fused up); the fusion then trims to
    top_k.
    """
    fetch_k = leg_k if leg_k is not None else max(top_k * 4, 20)
    doc_filter = "AND document_id = :document_id" if document_id is not None else ""
    params: dict[str, object] = {
        "qvec": _vector_literal(query_embedding),
        "q": query_text,
        "leg_k": fetch_k,
    }
    if document_id is not None:
        params["document_id"] = document_id

    # Dense leg: nearest neighbours by cosine distance. `embedding IS NOT NULL`
    # skips chunks not yet embedded (embedding is populated asynchronously).
    dense_sql = text(
        f"""
        SELECT id FROM chunks
        WHERE embedding IS NOT NULL {doc_filter}
        ORDER BY embedding <=> (:qvec)::vector
        LIMIT :leg_k
        """
    )
    # Sparse leg: full-text match ranked by ts_rank_cd. websearch_to_tsquery is the
    # forgiving parser (it never raises on user punctuation), which matters because
    # an Ask Me query is typed by a human.
    sparse_sql = text(
        f"""
        SELECT id FROM chunks
        WHERE content_tsv @@ websearch_to_tsquery('english', :q) {doc_filter}
        ORDER BY ts_rank_cd(content_tsv, websearch_to_tsquery('english', :q)) DESC
        LIMIT :leg_k
        """
    )

    dense_ids = [row[0] for row in (await session.execute(dense_sql, params)).all()]
    sparse_ids = [row[0] for row in (await session.execute(sparse_sql, params)).all()]

    fused = reciprocal_rank_fusion(
        {"dense": dense_ids, "sparse": sparse_ids},
        {"dense": weights.dense, "sparse": weights.sparse},
        k=k,
    )[:top_k]
    if not fused:
        return []

    rank_of = {chunk_id: i for i, (chunk_id, _) in enumerate(fused)}
    score_of = dict(fused)
    # One round-trip to hydrate the winners. ANY(:ids) is still RLS-filtered, so a
    # fused id that somehow named another org's row (it cannot -- the legs are
    # RLS-filtered too) would simply not come back.
    rows = (
        await session.execute(
            text(
                """
                SELECT id, content, document_id, page, char_start, char_end, element_type
                FROM chunks WHERE id = ANY(:ids)
                """
            ),
            {"ids": list(rank_of)},
        )
    ).all()

    hits = [
        ChunkHit(
            chunk_id=row.id,
            content=row.content,
            document_id=row.document_id,
            page=row.page,
            char_start=row.char_start,
            char_end=row.char_end,
            element_type=row.element_type,
            score=score_of[row.id],
        )
        for row in rows
    ]
    hits.sort(key=lambda hit: rank_of[hit.chunk_id])
    return hits


def register_pgvector_codec(engine) -> None:
    """Register pgvector's asyncpg codec on an async engine's connections (SIM-240
    sub-task 5, the design doc's named open technical item).

    hybrid_search sends the query vector as a cast text literal, so it is correct
    without this. Registering the codec lets embeddings cross as native binary
    arrays instead -- worth it once a 1024-dim vector is bound on a hot path, and
    required if a caller ever binds a vector parameter rather than a literal. Kept
    a lazy import so the module has no hard pgvector dependency at import time.
    """
    from pgvector.asyncpg import register_vector
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _register(dbapi_connection, _record):  # pragma: no cover - needs a live asyncpg conn
        dbapi_connection.await_(register_vector(dbapi_connection.driver_connection))
