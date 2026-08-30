"""AE-A-RETR-4 (SIM-241) memory scoping: the org-scoped retrieval seam every
feature (Ask Me, Portfolio-Fit) calls returns ONLY the caller's org's content and
fails closed when the session is scoped to the wrong org, or to no org at all.

This is the guard that sits ON TOP OF RLS. The DB-layer isolation of
`hybrid_search` is already proven in test_retrieval_rls.py; what these tests add
is the caller-seam property SIM-241 exists for: retrieval refuses to run when the
org the caller declares does not match the org the session is scoped to -- the
one cross-tenant failure mode plain RLS cannot catch, because RLS faithfully
serves whatever org the session names, even the wrong one for this caller.

Self-contained, matching test_retrieval_rls.py: it stands up a `chunks` table on
the FS-A-RETR-1 (SIM-237) contract with the org_isolation RLS policy, so it runs
against any pgvector Postgres. Skipped when none is reachable (needs
ALEMBIC_DATABASE_URL + DATABASE_URL, e.g. the local sandbox). Never touches a
cloud database.
"""

from __future__ import annotations

import os
from typing import TypedDict

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.core.exceptions import MemoryScopeError
from app.services.memory_scope import org_scoped_search
from tests.conftest import recreate_real_chunks_table

try:
    import psycopg2
except ImportError:  # pragma: no cover
    psycopg2 = None  # type: ignore[assignment]

ORG_A = "mem-scope-org-a"
ORG_B = "mem-scope-org-b"
SENTINEL_A = "ALPHAVAULT"  # appears only in org A's chunks
SENTINEL_B = "ZEBEDEE"  # appears only in org B's chunks -- must never surface for A


class _Query(TypedDict):
    query_text: str
    query_embedding: list[float]
    top_k: int


# Same words for both orgs, so only the org boundary -- not the content -- can
# keep them apart. That is the point of the test.
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
    assert psycopg2 is not None  # guarded by the _db_available skip below
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
            "INSERT INTO chunks (org_id, document_id, content, embedding, page,"
            " char_start, char_end, element_type)"
            " VALUES (%s, %s, %s, %s, %s, 0, 10, 'prose')",
            (org_id, doc, content, emb, page),
        )

    insert(
        org_ids[ORG_A],
        "docA",
        f"Total revenue grew strongly for the company {SENTINEL_A}",
        "[1,0,0,0]",
        1,
    )
    insert(
        org_ids[ORG_A],
        "docA",
        f"Operating margins were stable across segments {SENTINEL_A}",
        "[0,1,0,0]",
        2,
    )
    insert(
        org_ids[ORG_B],
        "docB",
        f"Total revenue grew strongly for the company {SENTINEL_B}",
        "[1,0,0,0]",
        1,
    )
    conn.close()

    yield org_ids

    conn = _owner_conn()
    cur = conn.cursor()
    recreate_real_chunks_table(cur)
    cur.execute("DELETE FROM organisation WHERE clerk_org_id IN (%s, %s)", (ORG_A, ORG_B))
    conn.close()


async def test_declared_org_receives_only_its_own_content(chunks_table) -> None:
    # The happy path through the seam: caller serving org A, on a session scoped
    # to org A, gets A's chunks and never B's -- checked by document AND by B's
    # unique sentinel token, which must not appear even indirectly.
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(text("SELECT set_config('app.org_id', :t, true)"), {"t": ORG_A})
        hits = await org_scoped_search(session, org_id=ORG_A, **QUERY)
    assert hits
    assert all(h.document_id == "docA" for h in hits)
    assert not any(SENTINEL_B in h.content for h in hits), (
        "org B's content must never surface for A"
    )


async def test_session_org_mismatch_fails_closed(chunks_table) -> None:
    # The failure RLS alone cannot catch: the session is scoped to org A, but the
    # caller declares it is serving org B. RLS would happily return A's chunks, and
    # a B-context caller would present them as B's -- a cross-tenant leak. The guard
    # refuses before any query runs.
    with pytest.raises(MemoryScopeError):
        async with AsyncSessionLocal() as session, session.begin():
            await session.execute(text("SELECT set_config('app.org_id', :t, true)"), {"t": ORG_A})
            await org_scoped_search(session, org_id=ORG_B, **QUERY)


async def test_unscoped_session_fails_closed() -> None:
    # Plain hybrid_search on an unscoped session silently returns [] (RLS matches
    # nothing). The seam turns that quiet nothing into a loud error, because an
    # unscoped session reaching retrieval is a wiring bug, not an empty result.
    # Needs no seeded data -- it fails before touching the chunks table.
    with pytest.raises(MemoryScopeError):
        async with AsyncSessionLocal() as session, session.begin():
            await org_scoped_search(session, org_id=ORG_A, **QUERY)
