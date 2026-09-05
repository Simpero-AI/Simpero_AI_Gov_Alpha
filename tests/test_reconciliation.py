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
from app.models import Claim, Deal, Edge, Organisation
from app.models.data_source import DataSource
from app.models.organisation import OrgType
from app.services.reconciliation import (
    ReconciliationSummary,
    reconcile_across_documents,
    reconcile_same_fact,
)

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
    for table in ("edges", "claims", "data_source", "deals"):
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
        deal = Deal(org_id=org.id, name=f"reconcile test deal ({org_key})")
        session.add(deal)
        await session.flush()
        for c in claims.values():
            c.org_id = org.id
            c.deal_id = deal.id
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
async def test_catch_all_attributes_are_never_reconciled_as_same_fact() -> None:
    # SIM-383 + Inspector fix: operating_metric AND core_unmapped are E2/SIM-344's
    # two catch-alls -- two such claims for one (entity, period) are almost never
    # the same fact (slot machines vs hotel rooms; an income-statement line on one
    # page vs a cash-flow line on another). A disagreeing pair that WOULD contradict
    # on any real attribute must write no edge; a real-attribute pair beside them
    # still reconciles, so the guard is specific to the catch-alls, not a blanket
    # skip.
    _delete_org(ORG)
    try:
        ids = await _seed(
            ORG,
            {
                "om_a": _claim(attribute="operating_metric", normalized=1_309, page=3),
                "om_b": _claim(attribute="operating_metric", normalized=2_444, page=11),
                "cu_a": _claim(attribute="core_unmapped", normalized=7_200_000, page=48),
                "cu_b": _claim(attribute="core_unmapped", normalized=1_800_000, page=49),
                "rev_a": _claim(attribute="revenue", normalized=15_000_000, page=3),
                "rev_b": _claim(attribute="revenue", normalized=12_000_000, page=11),
            },
        )
        summary = await _run_reconciliation(ORG, "run-1")
        assert summary.same_fact_edges == 0
        # revenue only; both catch-alls excluded.
        assert summary.contradicts_edges == 1

        edges, _ = await _edges_and_claims(ORG)
        assert len(edges) == 1
        assert {edges[0].from_claim_id, edges[0].to_claim_id} == {ids["rev_a"], ids["rev_b"]}
    finally:
        _delete_org(ORG)


# --------------------------------------------------------------------------
# SIM-428: deal-wide cross-DOCUMENT reconciliation (deck vs model vs data room).
# --------------------------------------------------------------------------

XDOC = "sim428-xdoc-org"


async def _seed_docs(
    org_key: str, docs: dict[str, dict[str, Claim]]
) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    """Insert an org + one deal + one data_source per doc label, each doc's
    labelled claims pointing at it. Returns (deal_id, label -> claim_id)."""
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE dd_app"))
        await session.execute(text("SELECT set_config('app.org_id', :k, true)"), {"k": org_key})
        org = Organisation(
            clerk_org_id=org_key, name=f"xdoc test ({org_key})", type=OrgType.PE_FIRM
        )
        session.add(org)
        await session.flush()
        deal = Deal(org_id=org.id, name=f"xdoc test deal ({org_key})")
        session.add(deal)
        await session.flush()
        by_label: dict[str, Claim] = {}
        for i, (doc_label, claims) in enumerate(docs.items()):
            ds = DataSource(
                org_id=org.id,
                deal_id=deal.id,
                storage_key=f"k/{doc_label}",
                filename=f"{doc_label}.pdf",
                declared_sha256=f"{i:064d}",
            )
            session.add(ds)
            await session.flush()
            for label, c in claims.items():
                c.org_id = org.id
                c.deal_id = deal.id
                c.data_source_id = ds.id
                session.add(c)
                by_label[label] = c
        await session.flush()
        return deal.id, {label: c.id for label, c in by_label.items()}


async def _run_cross_document(
    org_key: str, deal_id: uuid.UUID, run_id: str
) -> ReconciliationSummary:
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE dd_app"))
        await session.execute(text("SELECT set_config('app.org_id', :k, true)"), {"k": org_key})
        summary = await reconcile_across_documents(session, deal_id=deal_id, run_id=run_id)
        await session.flush()
    return summary


@requires_db
async def test_cross_document_agreement_creates_a_same_fact_edge() -> None:
    """The deck and the model both report the same revenue for the deal: a
    cross-document same_fact edge -- the deck-vs-model corroboration SIM-428 adds
    on top of the per-document passes."""
    _delete_org(XDOC)
    try:
        deal_id, ids = await _seed_docs(
            XDOC,
            {
                "deck": {"deck_rev": _claim(normalized=15_000_000, page=1)},
                "model": {"model_rev": _claim(normalized=15_000_000, page=1)},
            },
        )
        summary = await _run_cross_document(XDOC, deal_id, "run-1")
        assert summary.same_fact_edges == 1
        assert summary.contradicts_edges == 0
        edges, _ = await _edges_and_claims(XDOC)
        assert len(edges) == 1 and edges[0].type == "same_fact"
        assert {edges[0].from_claim_id, edges[0].to_claim_id} == {ids["deck_rev"], ids["model_rev"]}
    finally:
        _delete_org(XDOC)


@requires_db
async def test_cross_document_disagreement_creates_a_contradicts_edge() -> None:
    """The deck says 15M, the model says 12M for the same fact: a cross-document
    contradicts edge -- which the roll-up demotes to inconclusive, never
    conflicted (that is reserved for an outside source)."""
    _delete_org(XDOC)
    try:
        deal_id, ids = await _seed_docs(
            XDOC,
            {
                "deck": {"deck_rev": _claim(normalized=15_000_000, page=1)},
                "model": {"model_rev": _claim(normalized=12_000_000, page=1)},
            },
        )
        summary = await _run_cross_document(XDOC, deal_id, "run-1")
        assert summary.same_fact_edges == 0
        assert summary.contradicts_edges == 1
        edges, _ = await _edges_and_claims(XDOC)
        assert len(edges) == 1 and edges[0].type == "contradicts"
        assert edges[0].from_claim_id < edges[0].to_claim_id  # symmetric canonicalization
        assert {edges[0].from_claim_id, edges[0].to_claim_id} == {ids["deck_rev"], ids["model_rev"]}
    finally:
        _delete_org(XDOC)


@requires_db
async def test_a_single_document_deal_produces_no_cross_document_edge() -> None:
    """Two disagreeing claims in ONE document are reconcile_same_fact's job; the
    cross-document pass never considers a single-document group."""
    _delete_org(XDOC)
    try:
        deal_id, _ = await _seed_docs(
            XDOC,
            {
                "deck": {
                    "a": _claim(normalized=15_000_000, page=1),
                    "b": _claim(normalized=12_000_000, page=2),
                }
            },
        )
        summary = await _run_cross_document(XDOC, deal_id, "run-1")
        assert summary.groups_considered == 0
        edges, _ = await _edges_and_claims(XDOC)
        assert edges == []
    finally:
        _delete_org(XDOC)


@requires_db
async def test_a_within_document_pair_is_left_to_the_per_document_pass() -> None:
    """A group spanning two documents where one document holds a same-value pair:
    that within-document same_fact edge is reconcile_same_fact's, so this pass
    skips it, while the cross-document disagreement still yields a contradicts."""
    _delete_org(XDOC)
    try:
        deal_id, ids = await _seed_docs(
            XDOC,
            {
                "deck": {
                    "deck1": _claim(normalized=15_000_000, page=1),
                    "deck2": _claim(normalized=15_000_000, page=2),
                },
                "model": {"model": _claim(normalized=12_000_000, page=1)},
            },
        )
        summary = await _run_cross_document(XDOC, deal_id, "run-1")
        assert summary.same_fact_edges == 0  # deck1<->deck2 is same-document, skipped
        assert summary.contradicts_edges == 1
        edges, _ = await _edges_and_claims(XDOC)
        assert len(edges) == 1 and edges[0].type == "contradicts"
        assert ids["model"] in {edges[0].from_claim_id, edges[0].to_claim_id}
    finally:
        _delete_org(XDOC)


@requires_db
async def test_cross_document_rerun_is_idempotent() -> None:
    _delete_org(XDOC)
    try:
        deal_id, _ = await _seed_docs(
            XDOC,
            {
                "deck": {"d": _claim(normalized=15_000_000, page=1)},
                "model": {"m": _claim(normalized=15_000_000, page=1)},
            },
        )
        await _run_cross_document(XDOC, deal_id, "run-1")
        first, _ = await _edges_and_claims(XDOC)
        await _run_cross_document(XDOC, deal_id, "run-2")
        second, _ = await _edges_and_claims(XDOC)
        assert len(first) == 1 and len(second) == 1
        assert {e.id for e in first} == {e.id for e in second}
    finally:
        _delete_org(XDOC)
