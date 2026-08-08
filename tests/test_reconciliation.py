"""SIM-371: 3a reconciliation -- cross-page/tier same-fact. Agreement merges
into a SAME_FACT edge + flags the non-canonical claim; genuine disagreement
becomes a CONTRADICTS edge, never a silent merge; a same-page pair is left
alone (SIM-341's E1 reducer already owns it, per the SIM-370 decision); every
write is idempotent against SIM-369's UNIQUE constraint."""

from __future__ import annotations

import os
import uuid

import psycopg2
import pytest
from sqlalchemy import select, text

from app.core.database import AsyncSessionLocal
from app.models import Claim, Edge, Organisation
from app.models.organisation import OrgType
from app.services.reconciliation import ReconciliationSummary, reconcile_same_fact

ORG = "sim371-reconciliation-org"
OTHER = "sim371-reconciliation-other"


def _owner_dsn() -> str:
    return (
        os.environ.get("ALEMBIC_DATABASE_URL", "").replace("+psycopg2", "").replace("+asyncpg", "")
    )


def _db_available() -> bool:
    if not _owner_dsn():
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


def _delete_org(org_key: str) -> None:
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


def _claim(
    *,
    entity: str = "TestCo",
    attribute: str = "revenue",
    period_year: int = 2024,
    period_kind: str = "A",
    normalized: float,
    page: int | None,
    table_group_id: uuid.UUID | None = None,
    kind: str = "pdf",
    sheet: str | None = None,
    cell_ref: str | None = None,
) -> Claim:
    # A PDF claim carries a page + char span; an XLSX claim carries sheet/cell_ref
    # and no page (the locator CHECK enforces this; the span requirement exempts
    # xlsx). Page-less claims are what exercise the SIM-371 page-scope guard.
    location = (
        {"kind": "pdf", "page": page, "char_start": 0, "char_end": 1}
        if kind == "pdf"
        else {"kind": "xlsx", "sheet": sheet, "cell_ref": cell_ref}
    )
    return Claim(
        entity=entity,
        attribute=attribute,
        period_year=period_year,
        period_kind=period_kind,
        value={
            "raw": str(normalized),
            "normalized": normalized,
            "unit": "USD",
            "scale_multiplier": 1,
            "scale_source": "explicit_in_value",
            "value_type": "currency",
        },
        status="proposed",
        table_group_id=table_group_id,
        **location,
    )


async def _seed(org_key: str, claims: dict[str, Claim]) -> dict[str, uuid.UUID]:
    """Insert an org + labelled claims as dd_app under RLS, return label -> id."""
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE dd_app"))
        await session.execute(text("SELECT set_config('app.org_id', :k, true)"), {"k": org_key})
        org = Organisation(
            clerk_org_id=org_key, name=f"reconcile test ({org_key})", type=OrgType.PE_FIRM
        )
        session.add(org)
        await session.flush()
        for c in claims.values():
            c.org_id = org.id
        session.add_all(claims.values())
        await session.flush()
        return {label: c.id for label, c in claims.items()}


async def _run_reconciliation(org_key: str, run_id: str) -> ReconciliationSummary:
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE dd_app"))
        await session.execute(text("SELECT set_config('app.org_id', :k, true)"), {"k": org_key})
        summary: ReconciliationSummary = await reconcile_same_fact(
            session, data_source_id=None, run_id=run_id
        )
        await session.flush()
    return summary


async def _edges_and_claims(org_key: str) -> tuple[list[Edge], dict[uuid.UUID, Claim]]:
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(text("SELECT set_config('app.org_id', :k, true)"), {"k": org_key})
        edges = (await session.execute(select(Edge))).scalars().all()
        claims = (await session.execute(select(Claim))).scalars().all()
    return list(edges), {c.id: c for c in claims}


@requires_db
async def test_cross_page_same_fact_merges_and_flags_non_canonical() -> None:
    _delete_org(ORG)
    try:
        table_group = uuid.uuid4()
        ids = await _seed(
            ORG,
            {
                "table": _claim(normalized=15_000_000, page=3, table_group_id=table_group),
                "prose": _claim(normalized=15_000_000, page=11),
            },
        )
        summary = await _run_reconciliation(ORG, "run-1")
        assert summary.same_fact_edges == 1
        assert summary.contradicts_edges == 0
        assert summary.claims_flagged == 1

        edges, claims = await _edges_and_claims(ORG)
        same_fact = [e for e in edges if e.type == "same_fact"]
        assert len(same_fact) == 1
        # Table wins: to=table (canonical), from=prose (corroborator).
        assert same_fact[0].to_claim_id == ids["table"]
        assert same_fact[0].from_claim_id == ids["prose"]
        assert same_fact[0].created_by == "reconciliation"
        assert same_fact[0].run_id == "run-1"

        # KEEP BOTH: neither claim was dropped.
        assert set(claims) == {ids["table"], ids["prose"]}
        # Non-canonical (prose) carries the dumb-consumer guard flag; the
        # canonical (table) claim does not.
        assert "superseded_by_same_fact" in (claims[ids["prose"]].flags or [])
        assert "superseded_by_same_fact" not in (claims[ids["table"]].flags or [])
    finally:
        _delete_org(ORG)


@requires_db
async def test_cross_page_disagreement_flags_never_merges() -> None:
    _delete_org(ORG)
    try:
        ids = await _seed(
            ORG,
            {
                "table": _claim(normalized=15_000_000, page=3, table_group_id=uuid.uuid4()),
                "prose": _claim(normalized=12_000_000, page=11),
            },
        )
        summary = await _run_reconciliation(ORG, "run-1")
        assert summary.same_fact_edges == 0
        assert summary.contradicts_edges == 1
        assert summary.claims_flagged == 0

        edges, claims = await _edges_and_claims(ORG)
        contradicts = [e for e in edges if e.type == "contradicts"]
        assert len(contradicts) == 1
        c = contradicts[0]
        assert c.from_claim_id < c.to_claim_id, "contradicts must canonicalize from < to"
        assert {c.from_claim_id, c.to_claim_id} == {ids["table"], ids["prose"]}
        assert c.metadata_ is not None
        assert c.metadata_["value_delta"] != 0

        # Both claims persist untouched; disagreement is never resolved here.
        assert set(claims) == {ids["table"], ids["prose"]}
        assert not claims[ids["table"]].flags
        assert not claims[ids["prose"]].flags
    finally:
        _delete_org(ORG)


@requires_db
async def test_same_page_pair_is_left_to_the_e1_reducer() -> None:
    """SIM-370: E1 already writes within-page contradicts/same_fact. 3a is the
    cross-page generalization ONLY -- a same-page pair must be skipped, not
    re-derived, or the two passes would double-count the same relationship."""
    _delete_org(ORG)
    try:
        await _seed(
            ORG,
            {
                "table": _claim(normalized=15_000_000, page=5, table_group_id=uuid.uuid4()),
                "prose": _claim(normalized=12_000_000, page=5),  # SAME page
            },
        )
        summary = await _run_reconciliation(ORG, "run-1")
        assert summary.same_fact_edges == 0
        assert summary.contradicts_edges == 0
        assert summary.skipped_same_page_pairs == 1

        edges, _ = await _edges_and_claims(ORG)
        assert edges == []
    finally:
        _delete_org(ORG)


@requires_db
async def test_rerun_is_idempotent_via_the_unique_constraint() -> None:
    _delete_org(ORG)
    try:
        await _seed(
            ORG,
            {
                "table": _claim(normalized=15_000_000, page=3, table_group_id=uuid.uuid4()),
                "prose": _claim(normalized=15_000_000, page=11),
            },
        )
        await _run_reconciliation(ORG, "run-1")
        edges_first, _ = await _edges_and_claims(ORG)
        assert len(edges_first) == 1

        # Re-running over the SAME claims must not duplicate the edge -- the
        # ON CONFLICT DO NOTHING against uq_edges_org_from_to_type is what
        # makes this pass safe to run more than once over unchanged data.
        await _run_reconciliation(ORG, "run-2")
        edges_second, _ = await _edges_and_claims(ORG)
        assert len(edges_second) == 1
        assert edges_second[0].id == edges_first[0].id
        # The flag write is separately idempotent (checked before appending).
        _, claims = await _edges_and_claims(ORG)
    finally:
        _delete_org(ORG)


@requires_db
async def test_different_entity_or_period_is_never_reconciled() -> None:
    """Two claims that merely share a value are NOT the same fact -- grouping
    is entity+attribute+period, not value alone."""
    _delete_org(ORG)
    try:
        await _seed(
            ORG,
            {
                "a": _claim(entity="CompanyA", normalized=5_000_000, page=1),
                "b": _claim(entity="CompanyB", normalized=5_000_000, page=9),
            },
        )
        summary = await _run_reconciliation(ORG, "run-1")
        assert summary.groups_considered == 0
        edges, _ = await _edges_and_claims(ORG)
        assert edges == []
    finally:
        _delete_org(ORG)


@requires_db
async def test_rls_isolates_two_orgs() -> None:
    for org in (ORG, OTHER):
        _delete_org(org)
    try:
        await _seed(
            ORG,
            {
                "table": _claim(normalized=1_000, page=1, table_group_id=uuid.uuid4()),
                "prose": _claim(normalized=1_000, page=2),
            },
        )
        await _seed(
            OTHER,
            {
                "table": _claim(normalized=2_000, page=1, table_group_id=uuid.uuid4()),
                "prose": _claim(normalized=2_000, page=2),
            },
        )
        await _run_reconciliation(ORG, "run-1")
        await _run_reconciliation(OTHER, "run-1")

        org_edges, _ = await _edges_and_claims(ORG)
        other_edges, _ = await _edges_and_claims(OTHER)
        assert len(org_edges) == 1
        assert len(other_edges) == 1
        assert {e.id for e in org_edges}.isdisjoint({e.id for e in other_edges})
    finally:
        for org in (ORG, OTHER):
            _delete_org(org)


@requires_db
async def test_page_less_claims_are_left_to_e1() -> None:
    """XLSX/DOCX claims have no page, so 3a's cross-page scope excludes them
    entirely -- E1's within-'page' grouping owns them, and this pass must not also
    write a same_fact edge that the directional UNIQUE could not dedupe against
    E1's opposite-ordered one (the XLSX gap SIM-371's page scope closes)."""
    _delete_org(ORG)
    try:
        await _seed(
            ORG,
            {
                "s1": _claim(
                    normalized=15_000_000, page=None, kind="xlsx", sheet="Sheet1", cell_ref="B2"
                ),
                "s2": _claim(
                    normalized=15_000_000, page=None, kind="xlsx", sheet="Sheet2", cell_ref="B2"
                ),
            },
        )
        summary = await _run_reconciliation(ORG, "run-xlsx")
        # Page-less claims never even enter a group, so nothing is written.
        assert summary.groups_considered == 0
        assert summary.same_fact_edges == 0
        assert summary.contradicts_edges == 0

        edges, _ = await _edges_and_claims(ORG)
        assert edges == []
    finally:
        _delete_org(ORG)


@requires_db
async def test_operating_metric_is_never_reconciled_as_same_fact() -> None:
    # SIM-383: operating_metric is E2/SIM-344's catch-all -- two such claims for
    # one (entity, period) are almost never the same fact (one is slot machines,
    # the next is hotel rooms). A disagreeing pair that WOULD contradict on any
    # real attribute must write no edge; a real-attribute pair beside it still
    # reconciles, so the guard is specific to the catch-all, not a blanket skip.
    _delete_org(ORG)
    try:
        ids = await _seed(
            ORG,
            {
                "om_a": _claim(attribute="operating_metric", normalized=1_309, page=3),
                "om_b": _claim(attribute="operating_metric", normalized=2_444, page=11),
                "rev_a": _claim(attribute="revenue", normalized=15_000_000, page=3),
                "rev_b": _claim(attribute="revenue", normalized=12_000_000, page=11),
            },
        )
        summary = await _run_reconciliation(ORG, "run-1")
        assert summary.same_fact_edges == 0
        assert summary.contradicts_edges == 1  # revenue only; operating_metric excluded

        edges, _ = await _edges_and_claims(ORG)
        assert len(edges) == 1
        assert {edges[0].from_claim_id, edges[0].to_claim_id} == {ids["rev_a"], ids["rev_b"]}
    finally:
        _delete_org(ORG)
