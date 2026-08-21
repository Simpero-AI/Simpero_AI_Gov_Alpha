"""SIM-354 / L2: retrieval quality metrics -- recall@k and nDCG@k.

Pure and corpus-agnostic. The DB-driven CI runner (tests/test_l2_retrieval_eval)
and a future L3 real-CIM runner (SIM-357) both hand this ranked keys plus a
relevance labelling and get the same machine-readable report back (the SIM-355
artifact shape). Keeping it free of any DB or I/O is what lets the metric be
unit-tested deterministically and reused unchanged across the sparse-only CI
lane and the dense staging lane -- only the retrieval function upstream changes.

A "key" is any hashable identity for a retrievable unit (a chunk); the runner
chooses it (we join on chunk content) and the metric never inspects it.
Relevance is binary today -- a key is relevant to a query or it is not. Graded
gains are a later refinement the golden set does not yet label.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from math import log2
from typing import TypeVar

K = TypeVar("K", bound=Hashable)


def _top_k_distinct(ranked: Sequence[K], k: int) -> list[K]:
    """The first ``k`` DISTINCT keys of a ranking, first occurrence wins.

    A retrieval ranking is a ranking of distinct chunks, but a caller that joins
    retrieved hits back to keys by content (the L3 lane) can hand us the same key
    twice when two chunks share text. Deduping here keeps both metrics defined on
    distinct keys regardless of the caller's join -- without it, a repeated
    relevant key would be scored twice in the DCG sum and push nDCG above 1.0,
    out of its [0, 1] range. recall is unaffected (it counts the relevant set),
    but it routes through here too so the two metrics always see the same top k.
    """
    out: list[K] = []
    if k <= 0:
        # The truncation below only fires after a key is appended, so k=0 would
        # otherwise fall through and return the whole deduped ranking -- making
        # recall@0 report 1.0 for a top-0 that retrieved nothing. k=0 is a legal
        # input (the callers reject only k<0), so it must return the empty top.
        return out
    seen: set[K] = set()
    for key in ranked:
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) == k:
            break
    return out


def recall_at_k(ranked: Sequence[K], relevant: set[K], k: int) -> float:
    """Fraction of the relevant set present in the top ``k`` retrieved keys.

    An empty relevant set returns 1.0 (vacuously satisfied) so an unlabelled
    query cannot divide by zero; the golden set always labels >=1 relevant, so
    this branch never carries a real score.
    """
    if k < 0:
        raise ValueError("k must be non-negative")
    if not relevant:
        return 1.0
    top = _top_k_distinct(ranked, k)
    found = sum(1 for key in relevant if key in top)
    return found / len(relevant)


def ndcg_at_k(ranked: Sequence[K], relevant: set[K], k: int) -> float:
    """Binary-gain nDCG@k.

    DCG sums 1/log2(rank+1) over the relevant keys found in the top ``k`` (rank
    is 1-based); IDCG is that sum for the best possible ordering (every relevant
    key ranked first). nDCG is DCG/IDCG, so 1.0 means the relevant keys occupy
    the top positions and 0.0 means none were retrieved within ``k``. Unlike
    recall, this rewards ranking the relevant keys higher, not merely retrieving
    them -- a ranking regression that pushes a hit from rank 1 to rank 3 lowers
    nDCG while leaving recall@k flat.
    """
    if k < 0:
        raise ValueError("k must be non-negative")
    top = _top_k_distinct(ranked, k)
    dcg = sum(1.0 / log2(rank + 1) for rank, key in enumerate(top, start=1) if key in relevant)
    ideal_hits = min(len(relevant), k)
    idcg = sum(1.0 / log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0


def evaluate_retrieval(
    ranked_by_query: Mapping[str, Sequence[K]],
    relevant_by_query: Mapping[str, set[K]],
    ks: Sequence[int],
) -> dict:
    """Score every query at each k and return the machine-readable L2 report.

    Shape (the SIM-355 artifact seed; all k are stringified for JSON keys)::

        {
          "metric": "retrieval",
          "k_values": [1, 3, 5],
          "n_queries": 6,
          "per_query": [
            {"query_id": ..., "n_relevant": int, "n_retrieved": int,
             "recall": {"1": float, ...}, "ndcg": {"1": float, ...}},
            ...
          ],
          "aggregate": {"recall": {"1": float, ...}, "ndcg": {"1": float, ...}},
        }

    The aggregate is the macro-average (mean over queries), so each query counts
    once regardless of how many relevant chunks it carries -- a query with ten
    relevant chunks must not dominate the headline number. Deterministic:
    per_query is emitted in sorted query-id order.
    """
    if set(ranked_by_query) != set(relevant_by_query):
        raise ValueError("ranked_by_query and relevant_by_query must cover the same query ids")
    ks = sorted(set(ks))
    per_query: list[dict] = []
    for query_id in sorted(ranked_by_query):
        ranked = ranked_by_query[query_id]
        relevant = relevant_by_query[query_id]
        per_query.append(
            {
                "query_id": query_id,
                "n_relevant": len(relevant),
                "n_retrieved": len(ranked),
                "recall": {str(k): recall_at_k(ranked, relevant, k) for k in ks},
                "ndcg": {str(k): ndcg_at_k(ranked, relevant, k) for k in ks},
            }
        )

    def _mean(metric: str, k: int) -> float:
        if not per_query:
            return 0.0
        return sum(q[metric][str(k)] for q in per_query) / len(per_query)

    return {
        "metric": "retrieval",
        "k_values": ks,
        "n_queries": len(per_query),
        "per_query": per_query,
        "aggregate": {
            "recall": {str(k): _mean("recall", k) for k in ks},
            "ndcg": {str(k): _mean("ndcg", k) for k in ks},
        },
    }
