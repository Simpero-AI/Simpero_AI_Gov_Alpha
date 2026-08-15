"""SIM-352 / L1: the integrated asserting e2e runner.

The first check that runs the WHOLE ingest->retrieve seam against a real
pgvector Postgres and ASSERTS the result, instead of testing each half alone.
It ingests one document's claims AND chunks (the parser's real emitted output,
validated against contracts/{claims,chunks}.schema.json) under TWO tenants, then
runs hybrid_search and asserts the wiring the recon found fragile:

  1. claims and chunks land TOGETHER under the right tenant;
  2. a query built from a known claim retrieves the chunk carrying its text;
  3. two tenants holding IDENTICAL content stay isolated (RLS keeps them apart,
     not the content);
  4. an unscoped session (no app.org_id) sees nothing (fail-closed).

Sparse-only: chunks are ingested with a NULL embedding (no Voyage key), so the
dense leg (WHERE embedding IS NOT NULL) matches nothing and RRF degrades to the
full-text leg -- enough to prove retrieval is routed on real ingest output.

Skipped when no pgvector Postgres is reachable (run ./sandbox/up.sh); it never
touches a cloud database. In CI the DB is provided, so the skip must NOT fire --
see test_the_gate_runs_in_ci.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from app.core.database import AsyncSessionLocal
from app.models import Chunk, Claim, Deal, Organisation
from app.models.chunk import EMBEDDING_DIM
from app.models.organisation import OrgType
from app.services.retrieval import hybrid_search
from scripts.ingest_chunks import _row_from_chunk
from scripts.ingest_claims import _row_from_claim

try:
    import psycopg2
except ImportError:  # pragma: no cover
    psycopg2 = None  # type: ignore[assignment]

DATA = Path(__file__).parent / "data" / "e2e"
ORG = "l1-e2e-org"
OTHER_ORG = "l1-e2e-other"
# Never actually searched: every chunk is ingested embedding=NULL, so the dense
# leg matches zero rows and RRF is sparse-only. hybrid_search only rejects an
# EMPTY vector, so a right-dimension zero vector is a valid stand-in.
SPARSE_ONLY_QVEC = [0.0] * EMBEDDING_DIM


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


def test_the_gate_runs_in_ci() -> None:
    # The DB skip is a local-dev convenience. In CI the pgvector service is
    # provided, so a skip there would mean this gate silently never runs -- a
    # false-green. This test is deliberately NOT skip-guarded, so it fails loudly
    # if the DB is ever missing in CI.
    if os.environ.get("CI") and not _db_available():
        pytest.fail("L1 e2e gate skipped in CI: the pgvector DB must be reachable so it runs")


def _load(name: str) -> dict:
    return json.loads((DATA / f"{name}.json").read_text())


def _delete_org(org_key: str) -> None:
    conn = _owner_conn()
    cur = conn.cursor()
    for table in ("chunks", "claims", "deals"):
        cur.execute(
            f"DELETE FROM {table} WHERE org_id IN "
            "(SELECT id FROM organisation WHERE clerk_org_id = %s)",
            (org_key,),
        )
    cur.execute("DELETE FROM organisation WHERE clerk_org_id = %s", (org_key,))
    conn.close()


async def _ingest(org_key: str, claims: list[dict], chunks: list[dict]) -> None:
    """Ingest claims AND chunks under `org_key`, committed, exactly as the ingest
    scripts do (dd_app role + SET LOCAL app.org_id, so RLS applies to the org
    insert too) -- with NULL embeddings and a real commit so a later session can
    retrieve them. Reuses the scripts' real row translation."""
    session_id = uuid.uuid4()
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE dd_app"))
        await session.execute(text("SELECT set_config('app.org_id', :k, true)"), {"k": org_key})
        org = await session.scalar(select(Organisation).where(Organisation.clerk_org_id == org_key))
        if org is None:
            org = Organisation(
                clerk_org_id=org_key, name=f"L1 e2e ({org_key})", type=OrgType.PE_FIRM
            )
            session.add(org)
            await session.flush()
        org_id = org.id
        deal = await session.scalar(select(Deal).where(Deal.org_id == org_id))
        if deal is None:
            deal = Deal(org_id=org_id, name=f"L1 e2e deal ({org_key})")
            session.add(deal)
            await session.flush()
        deal_id = deal.id
        session.add_all(_row_from_claim(c, org_id, deal_id, session_id) for c in claims)
        for c in chunks:
            row = _row_from_chunk(c, org_id, embedding=None, embedding_version=None)
            # SIM-218 (migration 77be2ddc60a0) added the FK
            # chunks.document_id -> data_source(id). This direct-ingest path has no
            # real data_source row, so leave document_id NULL -- the FK only binds
            # when non-null, and this runner asserts on org + content, never
            # document_id (same treatment as test_chunks_rls). Wiring direct-ingest
            # to a seeded data_source row is the proper follow-up.
            row.document_id = None
            session.add(row)


async def _search_as(org_key: str, query_text: str):
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(text("SELECT set_config('app.org_id', :t, true)"), {"t": org_key})
        return await hybrid_search(
            session, query_text=query_text, query_embedding=SPARSE_ONLY_QVEC, top_k=10
        )


async def _search_unscoped(query_text: str):
    async with AsyncSessionLocal() as session, session.begin():
        return await hybrid_search(
            session, query_text=query_text, query_embedding=SPARSE_ONLY_QVEC, top_k=10
        )


@pytest.fixture
async def ingested():
    claims = _load("claims")["claims"]
    chunks = _load("chunks")["chunks"]
    # Two tenants hold IDENTICAL content, so only RLS -- not content -- can keep
    # them apart (see test_two_tenants... below). Clean both before and after.
    for org in (ORG, OTHER_ORG):
        _delete_org(org)
    await _ingest(ORG, claims, chunks)
    await _ingest(OTHER_ORG, claims, chunks)
    try:
        yield {"claims": claims, "chunks": chunks}
    finally:
        for org in (ORG, OTHER_ORG):
            _delete_org(org)


@requires_db
async def test_claims_and_chunks_land_together_under_the_tenant(ingested) -> None:
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(text("SELECT set_config('app.org_id', :t, true)"), {"t": ORG})
        n_claims = await session.scalar(select(func.count()).select_from(Claim))
        n_chunks = await session.scalar(select(func.count()).select_from(Chunk))
    assert n_claims == len(ingested["claims"])
    assert n_chunks == len(ingested["chunks"])


@requires_db
async def test_a_query_from_a_known_claim_retrieves_its_chunk(ingested) -> None:
    # The fixture's claim is about revenue $15,295; one chunk carries that exact
    # sentence. A query built from the claim must surface that chunk -- proving
    # retrieval is routed end-to-end on real emit->ingest output.
    hits = await _search_as(ORG, "revenue")
    assert hits, "sparse retrieval returned nothing for a term present in an ingested chunk"
    assert any("15,295" in h.content for h in hits), (
        "the chunk carrying the known claim's figure was not retrieved"
    )


@requires_db
async def test_two_tenants_holding_the_same_content_stay_isolated(ingested) -> None:
    # Both tenants hold the SAME ingested content, so a leak cannot hide behind
    # different text: each must retrieve its OWN chunk rows and never the other's.
    a = await _search_as(ORG, "revenue")
    b = await _search_as(OTHER_ORG, "revenue")
    assert a and b, "each tenant should retrieve its own copy of the revenue chunk"
    a_ids = {h.chunk_id for h in a}
    b_ids = {h.chunk_id for h in b}
    assert not (a_ids & b_ids), "a tenant retrieved another tenant's chunk rows -- RLS leak"


@requires_db
async def test_an_unscoped_session_sees_nothing(ingested) -> None:
    # No set_config -> app.org_id is NULL -> the policy matches no org -> zero
    # rows. The fail-closed property that makes a pooled connection safe: a
    # transaction that forgot to scope sees nothing, not everything.
    hits = await _search_unscoped("revenue")
    assert hits == []


def test_fixtures_conform_to_both_contracts() -> None:
    # The runner's input must be exactly what the parser emits, so validate the
    # committed fixtures against the shared contracts on every run (also exercises
    # the backend copy of chunks.schema.json -- SIM-353). No DB needed.
    from jsonschema import Draft202012Validator

    contracts = Path(__file__).parent.parent / "contracts"
    claim_v = Draft202012Validator(json.loads((contracts / "claims.schema.json").read_text()))
    chunk_v = Draft202012Validator(json.loads((contracts / "chunks.schema.json").read_text()))
    for claim in _load("claims")["claims"]:
        assert not sorted(claim_v.iter_errors(claim), key=str), (
            "fixture claim violates the contract"
        )
    for chunk in _load("chunks")["chunks"]:
        assert not sorted(chunk_v.iter_errors(chunk), key=str), (
            "fixture chunk violates the contract"
        )
