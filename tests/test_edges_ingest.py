"""SIM-366: edges ingest. The parser's same_fact/contradicts edges land in the
`edges` table, resolved claim_ref -> claim id. A skipped edge (a missing endpoint
or a contract-invalid shape) is non-fatal -- the document's claims still land --
and edges are org-isolated by RLS.

SIM-369 additions: every landed edge names its writer (created_by) and
authoring run (run_id); CONTRADICTS endpoints canonicalize to from < to by
UUID so both emission orders dedupe onto one row; the UNIQUE and no-self-edge
constraints are enforced at the DB level, independent of any one writer."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.database import AsyncSessionLocal
from app.models import Claim, Edge
from scripts.ingest_claims import _run

try:
    import psycopg2
except ImportError:  # pragma: no cover
    psycopg2 = None  # type: ignore[assignment]

ORG = "sim366-edges-org"
OTHER = "sim366-edges-other"

A = "1:10-17[#0]"
B = "1:400-410[#0]"
C = "1:800-810[#0]"
DANGLING = "9:999-999[#0]"  # never ingested -> a missing endpoint


def _owner_dsn() -> str:
    return (
        os.environ.get("ALEMBIC_DATABASE_URL", "").replace("+psycopg2", "").replace("+asyncpg", "")
    )


def _db_available() -> bool:
    if psycopg2 is None or not _owner_dsn():
        return False
    try:
        conn = psycopg2.connect(_owner_dsn())
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('public.edges')")
        row = cur.fetchone()
        conn.close()
        return row is not None and row[0] is not None
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not _db_available(), reason="no pgvector Postgres with the edges table (run ./sandbox/up.sh)"
)


def _claim(ref: str, char_start: int, char_end: int, normalized: int) -> dict:
    return {
        "claim_ref": ref,
        "claim_type": "numerical",
        "entity": "TestCo",
        "attribute": "revenue",
        "value": {
            "raw": f"${normalized}",
            "normalized": normalized,
            "unit": "USD",
            "scale_multiplier": 1,
            "scale_source": "explicit_in_value",
            "value_type": "currency",
        },
        "location": {
            "kind": "pdf",
            "file": "cim.pdf",
            "page": 1,
            "char_start": char_start,
            "char_end": char_end,
        },
        "status": "proposed",
        "verification_method": None,
        "flags": [],
    }


def _payload() -> dict:
    return {
        "claims": [_claim(A, 10, 17, 100), _claim(B, 400, 410, 120), _claim(C, 800, 810, 140)],
        "edges": [
            # resolvable same_fact -- the happy path for a type alpha ingest writes.
            {"type": "same_fact", "from": A, "to": B, "basis": "corroborates"},
            # resolvable contradicts -- same resolvability as the skip below, so the
            # only thing that differs from it is the endpoint, not the edge type.
            {"type": "contradicts", "from": B, "to": C, "basis": "disagree"},
            # missing endpoint: `to` was never ingested -> skipped, not fatal. Same
            # type as a landing edge, isolating resolvability as the one variable.
            {"type": "contradicts", "from": A, "to": DANGLING, "basis": "dangling"},
            # contract-invalid: no `to` key. A hard e["to"] read would raise and roll
            # back every claim; it must instead be skipped, non-fatally.
            {"type": "same_fact", "from": A, "basis": "malformed"},
            # contract-invalid: a `type` the CHECK would also reject. Caught at the
            # seam, before it can fail the insert.
            {"type": "not_a_real_type", "from": A, "to": B, "basis": "bad type"},
        ],
    }


def _delete_org(org_key: str) -> None:
    assert psycopg2 is not None
    conn = psycopg2.connect(_owner_dsn())
    conn.autocommit = True
    cur = conn.cursor()
    for table in ("edges", "claims"):
        cur.execute(
            f"DELETE FROM {table} WHERE org_id IN "
            "(SELECT id FROM organisation WHERE clerk_org_id = %s)",
            (org_key,),
        )
    cur.execute("DELETE FROM organisation WHERE clerk_org_id = %s", (org_key,))
    conn.close()


async def _edges_and_claim_refs(org_key: str) -> tuple[list[Edge], dict[str, uuid.UUID]]:
    """(edges, claim_ref -> claim id) as this tenant sees them, under RLS."""
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(text("SELECT set_config('app.org_id', :k, true)"), {"k": org_key})
        claims = (await session.execute(select(Claim))).scalars().all()
        edges = (await session.execute(select(Edge))).scalars().all()
    return list(edges), {c.claim_ref: c.id for c in claims if c.claim_ref is not None}


async def _insert_edge_as_dd_app(org_key: str, **edge_kwargs: object) -> None:
    """Insert one Edge exactly as the app would -- as dd_app, under this tenant's
    RLS scope -- so a constraint violation raised here is the same one the app
    would hit, not an artifact of testing as the table owner."""
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE dd_app"))
        await session.execute(text("SELECT set_config('app.org_id', :k, true)"), {"k": org_key})
        session.add(Edge(**edge_kwargs))
        await session.flush()


@requires_db
async def test_resolvable_edges_land_bad_ones_skip_and_rls_isolates_two_orgs() -> None:
    for org in (ORG, OTHER):
        _delete_org(org)
    try:
        await _run(_payload(), ORG, commit=True, session_id=uuid.uuid4())
        await _run(_payload(), OTHER, commit=True, session_id=uuid.uuid4())

        edges, ref_id = await _edges_and_claim_refs(ORG)

        # All three claims landed: the two skipped edges did not roll the doc back.
        assert set(ref_id) == {A, B, C}

        # Exactly the two RESOLVABLE, contract-valid edges landed -- both types,
        # each resolved claim_ref -> the right claim ids.
        assert len(edges) == 2
        same_fact = [e for e in edges if e.type == "same_fact"]
        contradicts = [e for e in edges if e.type == "contradicts"]
        # same_fact is directional and NOT reordered: `to` is always the
        # higher-precision source per the contract, landed exactly as authored
        # (the payload's same_fact is A->B).
        assert [(e.from_claim_id, e.to_claim_id, e.basis) for e in same_fact] == [
            (ref_id[A], ref_id[B], "corroborates")
        ]
        # contradicts is symmetric (SIM-369): its endpoints canonicalize to
        # from < to by UUID, so the ONLY thing assertable about which of B/C
        # landed as from vs to is the order invariant + the unordered pair --
        # not a fixed (B, C) tuple the way it was before canonicalization.
        assert len(contradicts) == 1
        c_edge = contradicts[0]
        assert c_edge.basis == "disagree"
        assert c_edge.from_claim_id < c_edge.to_claim_id
        assert {c_edge.from_claim_id, c_edge.to_claim_id} == {ref_id[B], ref_id[C]}

        # RLS: the other tenant has its OWN two edges, over its OWN claims, and the
        # id sets are disjoint -- a real second tenant, not an empty subquery.
        other_edges, other_ref_id = await _edges_and_claim_refs(OTHER)
        assert len(other_edges) == 2
        assert {e.id for e in edges}.isdisjoint({e.id for e in other_edges})
        assert {ref_id[A], ref_id[B], ref_id[C]}.isdisjoint(
            {other_ref_id[A], other_ref_id[B], other_ref_id[C]}
        )
        assert all(e.to_claim_id in set(other_ref_id.values()) for e in other_edges)
    finally:
        for org in (ORG, OTHER):
            _delete_org(org)


@requires_db
async def test_a_claim_with_a_live_edge_cannot_be_deleted_until_the_edge_is_gone() -> None:
    """The locked ON DELETE RESTRICT (deliberately not cascade): a claim an edge
    points at cannot be deleted while the edge stands. This is the constraint that
    forces re-ingest's ordered teardown -- drop edges, THEN claims (SIM-367)."""
    _delete_org(ORG)
    try:
        payload = {
            "claims": [_claim(A, 10, 17, 100), _claim(B, 400, 410, 120)],
            "edges": [{"type": "contradicts", "from": A, "to": B, "basis": "disagree"}],
        }
        await _run(payload, ORG, commit=True, session_id=uuid.uuid4())
        _, ref_id = await _edges_and_claim_refs(ORG)
        from_id = ref_id[A]

        assert psycopg2 is not None
        conn = psycopg2.connect(_owner_dsn())
        try:
            cur = conn.cursor()
            # Deleting the referenced claim while its edge stands is rejected:
            # SQLSTATE 23503 foreign_key_violation -- RESTRICT did its job.
            with pytest.raises(psycopg2.Error) as exc_info:
                cur.execute("DELETE FROM claims WHERE id = %s", (str(from_id),))
            assert exc_info.value.pgcode == "23503"
            conn.rollback()
            # And the ordered teardown succeeds: drop the edge first, then the claim
            # deletes cleanly -- exactly the order RESTRICT compels. Matches
            # BOTH from_claim_id and to_claim_id: SIM-369 canonicalizes
            # contradicts endpoints to from < to by UUID, so A can land on
            # either side depending on how it sorts against B.
            cur.execute(
                "DELETE FROM edges WHERE from_claim_id = %s OR to_claim_id = %s",
                (str(from_id), str(from_id)),
            )
            cur.execute("DELETE FROM claims WHERE id = %s", (str(from_id),))
            conn.commit()
        finally:
            conn.close()
    finally:
        _delete_org(ORG)


@requires_db
async def test_landed_edges_carry_created_by_and_run_id() -> None:
    """SIM-369: every edge names the pass that wrote it and its authoring run.
    This ingest path is the parser's E1 reducer only, so created_by is always
    'extraction_reducer'; run_id is the ingest's own session_id, letting two
    runs over the same document be told apart."""
    _delete_org(ORG)
    try:
        session_id = uuid.uuid4()
        await _run(_payload(), ORG, commit=True, session_id=session_id)
        edges, _ = await _edges_and_claim_refs(ORG)
        assert edges, "expected at least one landed edge"
        for e in edges:
            assert e.created_by == "extraction_reducer"
            assert e.run_id == str(session_id)
            # The parser doesn't supply rule/operand/value_delta metadata for
            # same_fact/contradicts -- nothing to carry on this path today.
            assert e.metadata_ is None
    finally:
        _delete_org(ORG)


@requires_db
async def test_contradicts_endpoints_are_canonically_ordered() -> None:
    """SIM-369: CONTRADICTS is symmetric (neither claim is preferred), so its
    endpoints are stored in from < to UUID order regardless of which order the
    parser emitted them in -- the invariant the UNIQUE constraint depends on to
    dedupe A<->B. same_fact is directional and must NOT be reordered: `to` is
    always the higher-precision source per the contract, not whichever UUID
    happens to sort second."""
    _delete_org(ORG)
    try:
        await _run(_payload(), ORG, commit=True, session_id=uuid.uuid4())
        edges, ref_id = await _edges_and_claim_refs(ORG)

        contradicts = [e for e in edges if e.type == "contradicts"]
        assert contradicts, "expected at least one landed contradicts edge"
        for e in contradicts:
            assert e.from_claim_id < e.to_claim_id, (
                "contradicts endpoints must canonicalize to from < to"
            )

        same_fact = [e for e in edges if e.type == "same_fact"]
        assert same_fact, "expected at least one landed same_fact edge"
        # _payload()'s same_fact is {"from": A, "to": B} -- assert it landed
        # exactly as authored (from=A/corroborator, to=B/source), NOT reordered
        # by UUID the way contradicts is.
        assert same_fact[0].from_claim_id == ref_id[A]
        assert same_fact[0].to_claim_id == ref_id[B]
    finally:
        _delete_org(ORG)


@requires_db
async def test_unique_constraint_rejects_a_duplicate_edge() -> None:
    """SIM-369: UNIQUE(org_id, from_claim_id, to_claim_id, type) is what makes an
    edge write idempotent -- a second identical edge is a conflict, not a second
    row. This tests the DB constraint directly (not through ingest re-run, which
    is SIM-367's ordered teardown and inserts fresh claim rows each time, so it
    cannot yet exercise this constraint end-to-end)."""
    _delete_org(ORG)
    try:
        await _run(_payload(), ORG, commit=True, session_id=uuid.uuid4())
        edges, ref_id = await _edges_and_claim_refs(ORG)
        e = edges[0]

        with pytest.raises(IntegrityError) as exc_info:
            await _insert_edge_as_dd_app(
                ORG,
                org_id=(await _org_id(ORG)),
                from_claim_id=e.from_claim_id,
                to_claim_id=e.to_claim_id,
                type=e.type,
                basis="duplicate",
                created_by="human",
            )
        assert "uq_edges_org_from_to_type" in str(exc_info.value.orig)
    finally:
        _delete_org(ORG)


@requires_db
async def test_self_edge_is_rejected() -> None:
    """SIM-369: a claim cannot relate to itself -- DB-enforced so a backend
    writer (reconciliation, consistency) that doesn't go through the parser's
    own self-contradicts guard still cannot create one."""
    _delete_org(ORG)
    try:
        await _run(_payload(), ORG, commit=True, session_id=uuid.uuid4())
        _, ref_id = await _edges_and_claim_refs(ORG)
        claim_id = ref_id[A]

        with pytest.raises(IntegrityError) as exc_info:
            await _insert_edge_as_dd_app(
                ORG,
                org_id=(await _org_id(ORG)),
                from_claim_id=claim_id,
                to_claim_id=claim_id,
                type="same_fact",
                basis="self",
                created_by="human",
            )
        assert "ck_edges_no_self_edge" in str(exc_info.value.orig)
    finally:
        _delete_org(ORG)


async def _org_id(org_key: str) -> int:
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(text("SELECT set_config('app.org_id', :k, true)"), {"k": org_key})
        from app.models import Organisation

        org = await session.scalar(select(Organisation).where(Organisation.clerk_org_id == org_key))
        assert org is not None
        return org.id
