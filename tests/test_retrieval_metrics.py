"""SIM-354 / L2: unit tests for the pure retrieval metrics.

No DB. These pin the metric definitions with hand-computed values so a change to
recall@k / nDCG@k is caught here, independently of the (skip-guarded) DB runner
that feeds them real hybrid_search output.
"""

from __future__ import annotations

import pytest

from app.eval.retrieval_metrics import evaluate_retrieval, ndcg_at_k, recall_at_k


def test_recall_counts_distinct_relevant_in_top_k() -> None:
    ranked = ["d", "a", "b"]
    relevant = {"a", "b"}
    assert recall_at_k(ranked, relevant, 1) == 0.0  # top1 = [d]
    assert recall_at_k(ranked, relevant, 2) == 0.5  # top2 = [d, a]
    assert recall_at_k(ranked, relevant, 3) == 1.0  # top3 = [d, a, b]


def test_recall_edge_cases() -> None:
    assert recall_at_k([], {"a"}, 5) == 0.0
    # An unlabelled query is vacuously satisfied, never a divide-by-zero.
    assert recall_at_k(["a"], set(), 5) == 1.0
    # k beyond the ranked length just takes what is there.
    assert recall_at_k(["a"], {"a"}, 10) == 1.0


def test_k_zero_retrieves_nothing() -> None:
    # k=0 is accepted (only k<0 raises), so it must mean "the top 0" -- an empty
    # retrieval that finds none of the relevant set. Getting this wrong reports a
    # perfect score for a lane that returned nothing, which is the one direction
    # a no-regression floor can never catch.
    assert recall_at_k(["a", "b"], {"a"}, 0) == 0.0
    assert ndcg_at_k(["a", "b"], {"a"}, 0) == 0.0
    # An unlabelled query stays vacuously satisfied, k notwithstanding.
    assert recall_at_k(["a"], set(), 0) == 1.0


def test_negative_k_is_rejected() -> None:
    with pytest.raises(ValueError):
        recall_at_k(["a"], {"a"}, -1)
    with pytest.raises(ValueError):
        ndcg_at_k(["a"], {"a"}, -1)


def test_ndcg_perfect_ordering_is_one() -> None:
    assert ndcg_at_k(["a", "b", "c"], {"a", "b"}, 3) == pytest.approx(1.0)


def test_ndcg_rewards_rank_position() -> None:
    # relevant a, b sit at ranks 2 and 3 behind a distractor d.
    # DCG  = 1/log2(3) + 1/log2(4) = 0.63093 + 0.5   = 1.13093
    # IDCG = 1/log2(2) + 1/log2(3) = 1.0     + 0.63093 = 1.63093
    assert ndcg_at_k(["d", "a", "b"], {"a", "b"}, 3) == pytest.approx(0.69342, abs=1e-4)


def test_ndcg_zero_when_nothing_relevant_retrieved_in_k() -> None:
    assert ndcg_at_k(["x", "y"], {"a"}, 2) == 0.0
    assert ndcg_at_k(["a", "x"], {"a"}, 1) == pytest.approx(1.0)


def test_ndcg_never_exceeds_one_with_a_repeated_key() -> None:
    # A content-join (the L3 lane) can hand the same key twice; a repeat must not
    # push nDCG above its [0, 1] range by double-counting it in the DCG sum.
    assert ndcg_at_k(["x", "x"], {"x"}, 2) == pytest.approx(1.0)
    assert ndcg_at_k(["x", "x", "y"], {"x", "y"}, 3) == pytest.approx(1.0)


def test_metrics_dedupe_ranked_so_distinct_keys_fill_top_k() -> None:
    # [x, x, y] has distinct top-2 {x, y}; the relevant y lands at distinct rank 2
    # rather than being crowded out by the duplicate x, and is scored once.
    assert recall_at_k(["x", "x", "y"], {"y"}, 2) == 1.0
    assert ndcg_at_k(["x", "x", "y"], {"y"}, 2) == pytest.approx(0.63093, abs=1e-4)


def test_evaluate_retrieval_macro_averages_and_shape() -> None:
    ranked = {"q1": ["a", "b"], "q2": ["x", "y"]}
    relevant = {"q1": {"a"}, "q2": {"y"}}
    report = evaluate_retrieval(ranked, relevant, ks=[1, 2])

    assert report["metric"] == "retrieval"
    assert report["k_values"] == [1, 2]
    assert report["n_queries"] == 2
    # Deterministic, sorted by query id.
    assert [q["query_id"] for q in report["per_query"]] == ["q1", "q2"]

    # q1: a at rank1 -> recall/ndcg 1.0 at both k.
    # q2: y at rank2 -> recall@1 0, recall@2 1; ndcg@1 0, ndcg@2 1/log2(3)=0.63093.
    agg = report["aggregate"]
    assert agg["recall"]["1"] == pytest.approx(0.5)  # (1.0 + 0.0)/2
    assert agg["recall"]["2"] == pytest.approx(1.0)  # (1.0 + 1.0)/2
    assert agg["ndcg"]["1"] == pytest.approx(0.5)  # (1.0 + 0.0)/2
    assert agg["ndcg"]["2"] == pytest.approx((1.0 + 0.63093) / 2, abs=1e-4)


def test_evaluate_retrieval_rejects_mismatched_query_sets() -> None:
    with pytest.raises(ValueError):
        evaluate_retrieval({"q1": ["a"]}, {"q2": {"a"}}, ks=[1])
