"""AE-A-RETR-3 live org isolation (sub-tasks 6 & 8): hybrid_search over a real
Postgres chunks table returns ONLY the caller's org's chunks, never another
org's, through the same org-scoped session (SET LOCAL app.org_id) production uses.

Self-contained: it creates a stand-in `chunks` table matching the FS-A-RETR-1
(SIM-237) contract -- id, org_id, document_id, content, embedding, content_tsv,
page, char span, element_type, with the org_isolation RLS policy -- so it runs
against any pgvector Postgres and does not wait on that migration. When SIM-237
lands the real table, delete the fixture and point these at it.

Skipped when no pgvector Postgres is reachable (needs ALEMBIC_DATABASE_URL +
DATABASE_URL, e.g. the local sandbox). It never touches a cloud database.
"""

from __future__ import annotations

import os
from typing import TypedDict

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.services.retrieval import hybrid_search
from tests.conftest import recreate_real_chunks_table

try:
    import psycopg2
except ImportError:  # pragma: no cover
    psycopg2 = None  # type: ignore[assignment]

ORG_A = "ret-test-org-a"
ORG_B = "ret-test-org-b"


class _Query(TypedDict):
    query_text: str
    query_embedding: list[float]
    top_k: int


QUERY: _Query = {
    "query_text": "total revenue company",
    "query_embedding": [1.0, 0.0, 0.0, 0.0],
    "top_k": 10,
}


def _owner_dsn() -> str:
    return (
        os.environ.get("ALEMBIC_DATABASE_URL", "").replace("+psycopg2", "").replace("+asyncpg", "")
    )


def _owner_conn():
    assert psycopg2 is not None  # guarded by the _db_available skip above
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
        ok = row is not None and row[0] == 1
        conn.close()
        return ok
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(),
    reason="no pgvector Postgres reachable (set ALEMBIC_DATABASE_URL/DATABASE_URL)",
)


@pytest.fixture
def chunks_table():
    conn = _owner_conn()
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS chunks CASCADE")
    cur.execute(
        """
        CREATE TABLE chunks (
            id           serial PRIMARY KEY,
            org_id       int NOT NULL REFERENCES organisation(id),
            document_id  text NOT NULL,
            content      text NOT NULL,
            embedding    vector(4),
            content_tsv  tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
            page         int NOT NULL,
            char_start   int,
            char_end     int,
            element_type text NOT NULL
        )
        """
    )
    cur.execute("CREATE INDEX ON chunks USING gin (content_tsv)")
    cur.execute("ALTER TABLE chunks ENABLE ROW LEVEL SECURITY")
    cur.execute(
        """
        CREATE POLICY org_isolation ON chunks FOR ALL TO dd_app
            USING (org_id IN (
                SELECT id FROM organisation
                WHERE clerk_org_id = current_setting('app.org_id', true)))
        """
    )
    cur.execute("GRANT SELECT, INSERT ON chunks TO dd_app")
    cur.execute("GRANT USAGE, SELECT ON SEQUENCE chunks_id_seq TO dd_app")

    org_ids: dict[str, int] = {}
    for key in (ORG_A, ORG_B):
        cur.execute("SELECT id FROM organisation WHERE clerk_org_id = %s", (key,))
        row = cur.fetchone()
        if row:
            org_ids[key] = row[0]
        else:
            cur.execute(
                "INSERT INTO organisation (name, type, clerk_org_id, created_at)"
                " VALUES (%s, 'PE_FIRM', %s, now()) RETURNING id",
                (key, key),
            )
            inserted = cur.fetchone()
            assert inserted is not None
            org_ids[key] = inserted[0]

    def insert(org_id: int, doc: str, content: str, emb: str, page: int) -> None:
        cur.execute(
            "INSERT INTO chunks (org_id, document_id, content, embedding, page, char_start, char_end, element_type)"
            " VALUES (%s, %s, %s, %s, %s, 0, 10, 'prose')",
            (org_id, doc, content, emb, page),
        )

    # Both orgs hold near-identical text, so only RLS -- not content -- can keep
    # them apart. That is the point: same words, different tenant.
    insert(org_ids[ORG_A], "docA", "Total revenue grew strongly for the company", "[1,0,0,0]", 1)
    insert(org_ids[ORG_A], "docA", "Operating margins were stable across segments", "[0,1,0,0]", 2)
    insert(org_ids[ORG_B], "docB", "Total revenue grew strongly for the company", "[1,0,0,0]", 1)
    conn.close()

    yield org_ids

    conn = _owner_conn()
    cur = conn.cursor()
    recreate_real_chunks_table(cur)
    cur.execute("DELETE FROM organisation WHERE clerk_org_id IN (%s, %s)", (ORG_A, ORG_B))
    conn.close()


async def _search_as(org_key: str, **kwargs):
    """Run hybrid_search exactly as get_db would: SET LOCAL app.org_id first."""
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(text("SELECT set_config('app.org_id', :t, true)"), {"t": org_key})
        return await hybrid_search(session, **kwargs)


async def test_each_org_sees_only_its_own_chunks(chunks_table) -> None:
    a_hits = await _search_as(ORG_A, **QUERY)
    b_hits = await _search_as(ORG_B, **QUERY)
    assert a_hits and all(h.document_id == "docA" for h in a_hits)
    assert b_hits and all(h.document_id == "docB" for h in b_hits)
    assert not ({h.chunk_id for h in a_hits} & {h.chunk_id for h in b_hits})


async def test_unset_org_sees_nothing_fail_closed(chunks_table) -> None:
    # No set_config -> current_setting('app.org_id', true) is NULL -> the policy
    # matches no org -> zero rows. The fail-closed property that makes a pooled
    # connection safe: a transaction that forgot to scope sees nothing, not everything.
    async with AsyncSessionLocal() as session, session.begin():
        hits = await hybrid_search(session, **QUERY)
    assert hits == []


async def test_sequential_org_transactions_never_leak(chunks_table) -> None:
    # SET LOCAL is transaction-scoped, so back-to-back org-scoped transactions on
    # reused backend connections (the PgBouncer case) never bleed into each other.
    first_a = await _search_as(ORG_A, **QUERY)
    b = await _search_as(ORG_B, **QUERY)
    second_a = await _search_as(ORG_A, **QUERY)
    assert all(h.document_id == "docA" for h in first_a)
    assert all(h.document_id == "docB" for h in b)
    assert all(h.document_id == "docA" for h in second_a)


async def test_document_id_filter_narrows_within_an_org(chunks_table) -> None:
    both = await _search_as(ORG_A, **QUERY)
    scoped = await _search_as(ORG_A, **{**QUERY, "document_id": "docA"})
    assert {h.document_id for h in scoped} == {"docA"}
    assert scoped and len(scoped) <= len(both)
