"""SIM-354 / L2: retrieval eval -- recall@k / nDCG@k over the synthetic golden
query set, sparse-only, asserted as a no-regression floor (seeds SIM-355).

Runs the same ingest -> hybrid_search seam as L1 (SIM-352), but instead of one
isolation assertion it scores whether the RIGHT chunks come back for a set of
labelled queries -- the instrument SIM-354 exists to add (nothing scored
retrieval quality before). Sparse-only in CI: no VOYAGE key, chunks ingested with
a NULL embedding, and the dense leg weight zeroed, so the score reflects the
full-text leg alone. The dense leg is L3's job (SIM-357: real embeddings on
staging), where this same metric code runs unchanged on real CIMs -- only the
retrieval function upstream changes.

One golden query ('profitability_hard') is expected to score near zero here
precisely because the sparse leg cannot bridge margin <-> profitability. That is
the dense leg's gap; the floor records it rather than hiding it.

Skipped when no pgvector Postgres is reachable (run ./sandbox/up.sh); it never
touches a cloud database. In CI the DB is provided, so the skip must NOT fire --
see test_the_l2_gate_runs_in_ci.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.core.database import AsyncSessionLocal
from app.eval.retrieval_metrics import evaluate_retrieval
from app.models import Organisation
from app.models.chunk import EMBEDDING_DIM
from app.models.organisation import OrgType
from app.services.retrieval import RRFWeights, hybrid_search
from scripts.ingest_chunks import _fold_content, _row_from_chunk

try:
    import psycopg2
except ImportError:  # pragma: no cover
    psycopg2 = None  # type: ignore[assignment]

DATA = Path(__file__).parent / "data" / "e2e"
ORG = "l2-retrieval-eval-org"
KS = (1, 3, 5)
# Sparse-only: dense leg dropped from the fusion (weight 0) AND embeddings are
# NULL, so the score is the full-text leg alone. hybrid_search still requires a
# non-empty query vector, so a right-dimension zero vector stands in.
SPARSE_ONLY = RRFWeights(dense=0.0, sparse=1.0)
SPARSE_ONLY_QVEC = [0.0] * EMBEDDING_DIM

# No-regression floor for the sparse-only lane, set from measurement on the
# pgvector sandbox (which matches CI's pgvector/pgvector:pg16). A score below any
# of these fails the gate. NOT aspirational -- see the printed report.
FLOOR_RECALL = {1: 0.75, 3: 5 / 6, 5: 5 / 6}
FLOOR_NDCG = {1: 5 / 6, 3: 5 / 6, 5: 5 / 6}

# The floors above are calibrated for exactly these six queries, one of which
# (profitability_hard) is the deliberate sparse-only miss. Pin the population so
# an edit to the golden set cannot silently re-base the floor -- deleting the
# zero-scoring query, for instance, would lift every aggregate past its floor
# with no signal that the hardest case vanished.
EXPECTED_QUERY_IDS = {
    "revenue_by_fiscal_year",
    "gross_margin",
    "headcount",
    "customer_concentration",
    "leverage",
    "profitability_hard",
}
DENSE_GAP_QUERY = "profitability_hard"


def _owner_dsn() -> str:
    return (
        os.environ.get("ALEMBIC_DATABASE_URL", "").replace("+psycopg2", "").replace("+asyncpg", "")
    )


def _owner_conn():
    assert psycopg2 is not None
    conn = psycopg2.connect(_owner_dsn())
    conn.autocommit = True
    return conn


def _db_available() -> bool:
    if psycopg2 is None or not _owner_dsn():
        return False
    try:
        conn = _owner_conn()
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
        row = cur.fetchone()
        conn.close()
        return row is not None and row[0] == 1
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not _db_available(),
    reason="no pgvector Postgres reachable (run ./sandbox/up.sh, or set ALEMBIC/DATABASE_URL)",
)


def _load_golden() -> dict:
    return json.loads((DATA / "golden_retrieval.json").read_text())


def _delete_org(org_key: str) -> None:
    conn = _owner_conn()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM chunks WHERE org_id IN (SELECT id FROM organisation WHERE clerk_org_id = %s)",
        (org_key,),
    )
    cur.execute("DELETE FROM organisation WHERE clerk_org_id = %s", (org_key,))
    conn.close()


async def _ingest(org_key: str, chunks: list[dict]) -> None:
    """Ingest the golden chunks under `org_key`, committed, exactly as the ingest
    script does (dd_app + SET LOCAL app.org_id) but with NULL embeddings -- the
    sparse-only lane. Retrieval needs no claims, so only chunks are ingested."""
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE dd_app"))
        await session.execute(text("SELECT set_config('app.org_id', :k, true)"), {"k": org_key})
        org = await session.scalar(select(Organisation).where(Organisation.clerk_org_id == org_key))
        if org is None:
            org = Organisation(
                clerk_org_id=org_key, name=f"L2 retrieval eval ({org_key})", type=OrgType.PE_FIRM
            )
            session.add(org)
            await session.flush()
        org_id = org.id
        for c in chunks:
            row = _row_from_chunk(c, org_id, embedding=None, embedding_version=None)
            # The eval corpus has no real data_source parent, so leave document_id
            # NULL: the FK chunks.document_id -> data_source(id) (migration
            # 77be2ddc60a0) only bites when non-null, and retrieval scores here by
            # org + content, never by document_id. Same treatment as
            # test_chunks_rls. The direct-ingest document_id path is exercised
            # elsewhere; wiring the eval corpus to a seeded data_source is a
            # separate follow-up (see the L1/data_source note).
            row.document_id = None
            session.add(row)


async def _search(org_key: str, query_text: str):
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(text("SELECT set_config('app.org_id', :t, true)"), {"t": org_key})
        return await hybrid_search(
            session,
            query_text=query_text,
            query_embedding=SPARSE_ONLY_QVEC,
            top_k=max(KS),
            weights=SPARSE_ONLY,
        )


def _maybe_write_artifact(report: dict) -> None:
    # SIM-355 seed: emit the machine-readable report when a collector asks for it
    # (EVAL_ARTIFACT_DIR set, e.g. in CI or the L3 workflow). A no-op otherwise.
    out_dir = os.environ.get("EVAL_ARTIFACT_DIR")
    if not out_dir:
        return
    path = Path(out_dir)
    path.mkdir(parents=True, exist_ok=True)
    (path / "l2_retrieval.json").write_text(
        json.dumps({"lane": "sparse_only", **report}, indent=2) + "\n"
    )


def test_the_l2_gate_runs_in_ci() -> None:
    # The DB skip is a local-dev convenience. In CI the pgvector service is
    # provided, so a skip there would silently drop this gate -- a false-green.
    if os.environ.get("CI") and not _db_available():
        pytest.fail("L2 retrieval gate skipped in CI: the pgvector DB must be reachable so it runs")


def test_golden_chunks_conform_to_contract() -> None:
    # The golden corpus must be exactly what the parser emits, so validate it
    # against the shared chunk contract on every run. No DB needed.
    from jsonschema import Draft202012Validator

    contracts = Path(__file__).parent.parent / "contracts"
    chunk_v = Draft202012Validator(json.loads((contracts / "chunks.schema.json").read_text()))
    for chunk in _load_golden()["chunks"]:
        assert not sorted(chunk_v.iter_errors(chunk), key=str), "golden chunk violates the contract"


@requires_db
async def test_retrieval_meets_the_no_regression_floor() -> None:
    gold = _load_golden()
    chunks = gold["chunks"]
    # Folded content is the stable relevance key (chunk index). It must be unique
    # or two chunks would collapse to one key and the join would be ambiguous.
    key_of = {_fold_content(c): i for i, c in enumerate(chunks)}
    assert len(key_of) == len(chunks), (
        "golden chunk contents must be unique (they are the join key)"
    )

    _delete_org(ORG)
    try:
        await _ingest(ORG, chunks)
        ranked_by_query: dict[str, list[int]] = {}
        relevant_by_query: dict[str, set[int]] = {}
        for q in gold["queries"]:
            hits = await _search(ORG, q["query_text"])
            ranked = [key_of[h.content] for h in hits if h.content in key_of]
            # Only golden chunks live under this org, so every hit must join back.
            # A dropped hit would shorten the ranking silently, promoting the hits
            # behind it and inflating both metrics -- the gate would read greener
            # the more the join broke. Fail loudly instead.
            assert len(ranked) == len(hits), (
                f"query {q['id']!r}: {len(hits) - len(ranked)} retrieved chunk(s) did not "
                "join back to the golden corpus"
            )
            ranked_by_query[q["id"]] = ranked
            relevant_by_query[q["id"]] = set(q["relevant"])
        report = evaluate_retrieval(ranked_by_query, relevant_by_query, ks=KS)
    finally:
        _delete_org(ORG)

    _maybe_write_artifact(report)
    print("\nL2 sparse-only retrieval report:\n" + json.dumps(report, indent=2))

    query_ids = [q["id"] for q in gold["queries"]]
    assert len(set(query_ids)) == len(query_ids), "golden query ids must be unique"
    assert set(query_ids) == EXPECTED_QUERY_IDS, (
        "golden query set changed; recalibrate the floors before editing the population"
    )
    assert report["n_queries"] == len(EXPECTED_QUERY_IDS)
    # Exactly the one dense-leg-gap query may miss entirely under sparse-only. A
    # second query regressing to zero shows up here, and deleting the gap query
    # empties this list -- either way the gate fails instead of averaging the loss
    # away in the aggregate.
    zero_at_max_k = [q["query_id"] for q in report["per_query"] if q["recall"][str(max(KS))] == 0.0]
    assert zero_at_max_k == [DENSE_GAP_QUERY], (
        f"expected only {DENSE_GAP_QUERY!r} to miss sparse-only, got {zero_at_max_k}"
    )

    agg = report["aggregate"]
    for k in KS:
        assert agg["recall"][str(k)] >= FLOOR_RECALL[k] - 1e-9, (
            f"recall@{k}={agg['recall'][str(k)]:.4f} below floor {FLOOR_RECALL[k]}\n{report}"
        )
        assert agg["ndcg"][str(k)] >= FLOOR_NDCG[k] - 1e-9, (
            f"ndcg@{k}={agg['ndcg'][str(k)]:.4f} below floor {FLOOR_NDCG[k]}\n{report}"
        )
