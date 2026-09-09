"""AE-A-RETR-3 hybrid retrieval: fusion correctness, injectable weighting, and
the query shape (org-scoped session, document_id filter) -- all without a DB.

The live org-isolation-under-pooling test is tests/test_retrieval_rls.py, which
needs the chunks table (SIM-237) and a Postgres, and is skipped without them.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.retrieval import (
    RRFWeights,
    hybrid_search,
    reciprocal_rank_fusion,
)

# --------------------------------------------------------------------------- #
# Reciprocal Rank Fusion -- pure logic
# --------------------------------------------------------------------------- #


def test_a_chunk_in_both_legs_outscores_one_in_a_single_leg() -> None:
    fused = reciprocal_rank_fusion(
        {"dense": [1, 2, 3], "sparse": [2, 4]}, {"dense": 1.0, "sparse": 1.0}
    )
    assert fused[0][0] == 2  # the only chunk surfaced by both legs


def test_weighting_is_injectable_and_flips_the_winner() -> None:
    # Two chunks, mirror-image ranks: 10 leads the dense leg, 20 leads the sparse.
    legs = {"dense": [10, 20], "sparse": [20, 10]}
    dense_heavy = reciprocal_rank_fusion(legs, {"dense": 1.0, "sparse": 0.1})
    sparse_heavy = reciprocal_rank_fusion(legs, {"dense": 0.1, "sparse": 1.0})
    assert dense_heavy[0][0] == 10
    assert sparse_heavy[0][0] == 20


def test_zero_weight_drops_a_leg_entirely() -> None:
    fused = reciprocal_rank_fusion(
        {"dense": [1, 2], "sparse": [3, 4]}, {"dense": 1.0, "sparse": 0.0}
    )
    assert {chunk_id for chunk_id, _ in fused} == {1, 2}


def test_empty_legs_return_empty() -> None:
    assert reciprocal_rank_fusion({"dense": [], "sparse": []}, {"dense": 1.0, "sparse": 1.0}) == []


def test_ties_are_broken_by_chunk_id_for_stable_output() -> None:
    # 7 and 4 are each rank-1 in their own leg -> identical score -> lower id first.
    fused = reciprocal_rank_fusion({"a": [7], "b": [4]}, {"a": 1.0, "b": 1.0})
    assert [chunk_id for chunk_id, _ in fused] == [4, 7]


def test_k_is_injectable_and_larger_k_compresses_rank_gaps() -> None:
    small_k = reciprocal_rank_fusion({"d": [1, 2]}, {"d": 1.0}, k=1)
    large_k = reciprocal_rank_fusion({"d": [1, 2]}, {"d": 1.0}, k=1000)
    gap_small = small_k[0][1] - small_k[1][1]
    gap_large = large_k[0][1] - large_k[1][1]
    assert gap_large < gap_small


# --------------------------------------------------------------------------- #
# hybrid_search -- query shape, on a recording fake session (no DB)
# --------------------------------------------------------------------------- #


class _Result:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows


class RecordingSession:
    """Stands in for an org-scoped AsyncSession, recording every statement so the
    test can assert the query shape without a database. Returns the dense leg for
    the `<=>` query, the sparse leg for the `@@` query, and hydrated rows for the
    final id lookup."""

    def __init__(
        self, dense_ids: list[int], sparse_ids: list[int], rows: Mapping[int, object]
    ) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._dense = dense_ids
        self._sparse = sparse_ids
        self._rows = rows

    async def execute(self, statement, params: dict | None = None):  # noqa: ANN001
        sql = str(statement)
        self.calls.append((sql, dict(params or {})))
        if "<=>" in sql:
            return _Result([(i,) for i in self._dense])
        if "@@" in sql:
            return _Result([(i,) for i in self._sparse])
        assert params is not None
        ids = params["ids"]
        return _Result([self._rows[i] for i in ids if i in self._rows])


def _row(chunk_id: int, document_id: str = "DOC") -> SimpleNamespace:
    return SimpleNamespace(
        id=chunk_id,
        content=f"chunk {chunk_id}",
        document_id=document_id,
        page=chunk_id,
        char_start=0,
        char_end=5,
        element_type="prose",
    )


async def _run(session: RecordingSession, **kwargs):
    """hybrid_search with the recording fake, which stands in for the
    AsyncSession the production signature asks for."""
    return await hybrid_search(cast(AsyncSession, session), **kwargs)


async def test_hybrid_search_returns_fused_order_and_hydrates_rows() -> None:
    rows = {1: _row(1), 2: _row(2), 3: _row(3)}
    # 2 leads dense, 1 leads sparse; equal weights -> tie between 1 and 2, id-broken.
    session = RecordingSession(dense_ids=[2, 1, 3], sparse_ids=[1, 2], rows=rows)
    hits = await _run(session, query_text="revenue", query_embedding=[0.1, 0.2, 0.3], top_k=3)
    assert [h.chunk_id for h in hits] == [1, 2, 3]
    assert hits[0].content == "chunk 1"
    assert all(h.score > 0 for h in hits)


async def test_hybrid_search_weighting_flips_the_top_hit() -> None:
    rows = {10: _row(10), 20: _row(20)}
    session_dense = RecordingSession(dense_ids=[10, 20], sparse_ids=[20, 10], rows=rows)
    session_sparse = RecordingSession(dense_ids=[10, 20], sparse_ids=[20, 10], rows=rows)
    dense_hits = await _run(
        session_dense,
        query_text="q",
        query_embedding=[1.0],
        top_k=2,
        weights=RRFWeights(dense=1.0, sparse=0.1),
    )
    sparse_hits = await _run(
        session_sparse,
        query_text="q",
        query_embedding=[1.0],
        top_k=2,
        weights=RRFWeights(dense=0.1, sparse=1.0),
    )
    assert dense_hits[0].chunk_id == 10
    assert sparse_hits[0].chunk_id == 20


async def test_dense_and_sparse_legs_have_the_right_operators() -> None:
    session = RecordingSession(dense_ids=[1], sparse_ids=[1], rows={1: _row(1)})
    await _run(session, query_text="margin", query_embedding=[0.5, 0.5], top_k=1)
    dense_sql = next(sql for sql, _ in session.calls if "<=>" in sql)
    sparse_sql = next(sql for sql, _ in session.calls if "@@" in sql)
    assert "(:qvec)::vector" in dense_sql
    assert "websearch_to_tsquery" in sparse_sql and "ts_rank_cd" in sparse_sql


async def test_document_id_filters_both_legs_when_given_and_not_otherwise() -> None:
    rows = {1: _row(1, document_id="D1")}
    scoped = RecordingSession(dense_ids=[1], sparse_ids=[1], rows=rows)
    await _run(scoped, query_text="q", query_embedding=[1.0], top_k=1, document_id="D1")
    leg_calls = [(sql, p) for sql, p in scoped.calls if "<=>" in sql or "@@" in sql]
    assert leg_calls, "expected dense and sparse legs"
    for sql, params in leg_calls:
        assert "document_id = :document_id" in sql
        assert params["document_id"] == "D1"

    unscoped = RecordingSession(dense_ids=[1], sparse_ids=[1], rows=rows)
    await _run(unscoped, query_text="q", query_embedding=[1.0], top_k=1)
    for sql, params in [(sql, p) for sql, p in unscoped.calls if "<=>" in sql or "@@" in sql]:
        assert "document_id" not in sql
        assert "document_id" not in params


async def test_no_matches_returns_empty_without_hydrating() -> None:
    session = RecordingSession(dense_ids=[], sparse_ids=[], rows={})
    hits = await _run(session, query_text="q", query_embedding=[1.0], top_k=5)
    assert hits == []
    # only the two legs ran; no id-hydration round-trip on an empty fusion
    assert all("ANY(:ids)" not in sql for sql, _ in session.calls)


async def test_document_ids_filter_both_legs_with_any() -> None:
    # A whole DEAL's document set: one ANY(:document_ids) filter on both legs, one
    # global fusion -- the shape a deal-scoped consumer (field synthesis) needs.
    rows = {1: _row(1, document_id="D1")}
    session = RecordingSession(dense_ids=[1], sparse_ids=[1], rows=rows)
    await _run(session, query_text="q", query_embedding=[1.0], top_k=1, document_ids=["D1", "D2"])
    leg_calls = [(sql, p) for sql, p in session.calls if "<=>" in sql or "@@" in sql]
    assert leg_calls, "expected dense and sparse legs"
    for sql, params in leg_calls:
        assert "document_id = ANY(:document_ids)" in sql
        assert params["document_ids"] == ["D1", "D2"]


async def test_no_embedding_runs_sparse_only() -> None:
    # Before embeddings are backfilled a caller omits the vector; the dense leg is
    # skipped entirely (not passed a dummy vector, which _vector_literal rejects),
    # and the sparse leg still returns results.
    rows = {1: _row(1), 2: _row(2)}
    session = RecordingSession(dense_ids=[99], sparse_ids=[1, 2], rows=rows)
    hits = await _run(session, query_text="q", query_embedding=None, top_k=2)
    assert [h.chunk_id for h in hits] == [1, 2]  # sparse order; dense never ran
    assert all("<=>" not in sql for sql, _ in session.calls)


async def test_dense_leg_skipped_when_its_weight_is_zero() -> None:
    # Zeroing the dense weight drops the leg outright, so no vector query runs even
    # if a vector was passed.
    rows = {1: _row(1)}
    session = RecordingSession(dense_ids=[1], sparse_ids=[1], rows=rows)
    await _run(
        session,
        query_text="q",
        query_embedding=[1.0],
        top_k=1,
        weights=RRFWeights(dense=0.0, sparse=1.0),
    )
    assert all("<=>" not in sql for sql, _ in session.calls)


def test_empty_embedding_runs_sparse_only_rather_than_raising() -> None:
    # An empty vector is treated the same as no vector: sparse-only, no raise.
    import asyncio

    session = RecordingSession(dense_ids=[], sparse_ids=[1], rows={1: _row(1)})
    hits = asyncio.run(_run(session, query_text="q", query_embedding=[], top_k=1))
    assert [h.chunk_id for h in hits] == [1]
    assert all("<=>" not in sql for sql, _ in session.calls)
