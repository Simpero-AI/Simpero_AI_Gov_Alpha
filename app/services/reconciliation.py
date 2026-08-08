"""SIM-371: 3a Reconciliation -- cross-page/tier same-fact reconciliation (alpha).

The narrowest instance of this problem (within-page, same page, two tiers)
already lives in the parser's E1 reducer (SIM-341), persisted via
scripts/ingest_claims.py as `same_fact`/`contradicts` edges with
created_by='extraction_reducer'. This module is the CROSS-PAGE
generalization: two claims naming the same entity/attribute/period on
DIFFERENT pages (a summary table on page 3 restating a number the MD&A
prose gives on page 11).

Deliberately scoped to cross-page pairs only (never same-page): a same-page
pair is E1's job, already done and already an edge. Re-deriving it here
would double-count -- SIM-370 decided the parser's within-page contradicts
stays, and this module's cross-page-only scope is what keeps 3a from
stepping on it, without needing to inspect E1's edges directly.

KEEP BOTH, NEVER DROP-THE-LOSER (design doc Step 3a, Section 5): every claim
this pass looks at persists exactly as ingested. Agreement produces a
SAME_FACT edge and a `superseded_by_same_fact` flag on the non-canonical
claim (the dumb-consumer guard -- an edge-ignorant reader skips a flagged
claim rather than double-counting it as independent corroboration).
Disagreement produces a CONTRADICTS edge and flags nothing -- the
disagreement itself is the useful signal, per the contract's own edge
description.

OWNERSHIP / TABLE-WINS-ON-NUMBERS (open question, see below): the design
doc says ownership is decided by the chunker's `element_type` (a table
chunk vs a prose chunk). `Claim.chunk_id` exists but nothing populates it
yet (verified against origin/staging at the time this was written), so in
practice this falls through to the fallback below for every claim today.
Flagging this openly rather than presenting it as settled: whether Docling's
`element_type` classification is clean enough to be the ownership line is
explicitly called out in SIM-371 as an open, unmeasured question -- this
pass does not resolve it, only the fallback that lets it produce something
useful in the meantime.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Claim, Edge

# Same real-world number restated twice should match closely -- this is NOT
# the arithmetic-derivation tolerance SIM-372 uses (that checks a formula
# reconstruction, a different question). A same-fact match tolerates only
# float/rounding jitter; anything wider is a genuine disagreement, which is
# exactly the case this pass must flag, not paper over.
_SAME_FACT_REL_TOL = 0.001
_SAME_FACT_ABS_TOL = 0.01


def _values_match(a: float, b: float) -> bool:
    if a == b:
        return True
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) <= max(_SAME_FACT_ABS_TOL, _SAME_FACT_REL_TOL * scale)


def _is_table_sourced(claim: Claim) -> bool:
    """Best-effort ownership signal. Prefers the chunk's element_type (the
    design doc's intended signal) when a claim actually has a chunk_id to
    look up -- but resolving that requires a join this pass does not do
    today (chunk_id is unpopulated on every claim as of this writing, so the
    join would be a no-op in practice; adding it now would be untested
    dead code). Falls back to `table_group_id is not None`, which IS
    populated by the in-repo table-fragment grouping and is a real, if
    coarser, table-vs-prose signal available right now."""
    return claim.table_group_id is not None


def _canonical_from_to(claim_a: uuid.UUID, claim_b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """CONTRADICTS is symmetric (SIM-369): canonicalize to from < to so the
    UNIQUE constraint dedupes A<->B regardless of which claim this pass
    visited first."""
    return (claim_a, claim_b) if claim_a < claim_b else (claim_b, claim_a)


@dataclass
class ReconciliationSummary:
    same_fact_edges: int = 0
    contradicts_edges: int = 0
    claims_flagged: int = 0
    groups_considered: int = 0
    skipped_same_page_pairs: int = 0
    open_questions: list[str] = field(default_factory=list)


async def reconcile_same_fact(
    session: AsyncSession,
    *,
    data_source_id: uuid.UUID | None,
    run_id: str,
) -> ReconciliationSummary:
    """Cross-page/tier same-fact reconciliation over one document's claims.

    `session` must already be RLS-scoped (SET LOCAL app.org_id) by the
    caller, same contract as app/services/memory_scope.py -- this function
    does not scope it itself. `data_source_id` narrows to one document
    (reconciliation is a within-document operation: two different CIMs
    sharing an entity/attribute/period by coincidence are not "the same
    fact"); pass None for the demo/no-data_source_id ingest path, which
    matches every claim with data_source_id IS NULL.

    Idempotent: edge writes go through INSERT ... ON CONFLICT DO NOTHING
    against SIM-369's UNIQUE(org_id, from, to, type), so a re-run over
    unchanged claims writes zero new rows. The `superseded_by_same_fact`
    flag write is naturally idempotent too (skipped if already present).
    """
    stmt = select(Claim).where(Claim.value["normalized"].isnot(None))
    stmt = stmt.where(
        Claim.data_source_id.is_(None)
        if data_source_id is None
        else Claim.data_source_id == data_source_id
    )
    # SIM-371: 3a is CROSS-PAGE reconciliation, and the within-vs-cross split that
    # keeps it from double-counting E1's within-page edges is decided by comparing
    # `page` (see _reconcile_group's same-page skip). A page-less claim -- an XLSX
    # cell or a DOCX paragraph -- has no page to compare, so that guard cannot fire
    # for it, and E1 and this pass could each write a same_fact edge for the same
    # pair in opposite from/to order that the (directional) UNIQUE would not dedupe.
    # Scope to paged claims: page-less claims stay entirely E1's. Cross-sheet XLSX
    # reconciliation, if ever needed, is a separate pass, not this one.
    stmt = stmt.where(Claim.page.isnot(None))
    # Qualitative claims carry no magnitude (value.normalized is null by
    # construction), already excluded by the filter above. claim_kind is
    # otherwise irrelevant here: numeric facts, not extraction provenance.
    claims = list((await session.scalars(stmt)).all())

    groups: dict[tuple[str, str, int | None, str | None], list[Claim]] = {}
    for c in claims:
        groups.setdefault((c.entity, c.attribute, c.period_year, c.period_kind), []).append(c)

    summary = ReconciliationSummary()
    for group in groups.values():
        if len(group) < 2:
            continue
        summary.groups_considered += 1
        await _reconcile_group(session, group, run_id=run_id, summary=summary)

    summary.open_questions.append(
        "table-vs-prose ownership falls back to table_group_id today -- "
        "chunk.element_type is unpopulated, so the design doc's intended "
        "signal is not yet exercised. Needs measurement against the golden "
        "set once the chunker populates claim.chunk_id (see SIM-371's own "
        "open question)."
    )
    return summary


async def _reconcile_group(
    session: AsyncSession,
    group: Sequence[Claim],
    *,
    run_id: str,
    summary: ReconciliationSummary,
) -> None:
    # Greedy value-clustering: each claim joins the first existing cluster
    # whose representative value matches it, else starts a new cluster.
    # Small per-document groups (a handful of claims sharing entity/attribute/
    # period), so this is O(n * clusters), not a concern at this scale.
    clusters: list[list[Claim]] = []
    for c in group:
        value = c.value.get("normalized")
        if value is None:
            continue
        placed = False
        for cluster in clusters:
            rep = cluster[0].value["normalized"]
            if _values_match(float(rep), float(value)):
                cluster.append(c)
                placed = True
                break
        if not placed:
            clusters.append([c])

    if not clusters or (len(clusters) < 2 and len(clusters[0]) < 2):
        return  # nothing to reconcile: no/one claim, or already a single value

    def cluster_rank(cluster: list[Claim]) -> tuple[bool, int, uuid.UUID]:
        # Table-containing cluster wins; then largest cluster; then lowest
        # id, for a fully deterministic pick (no arbitrary dict/set order).
        has_table = any(_is_table_sourced(c) for c in cluster)
        return (not has_table, -len(cluster), min(c.id for c in cluster))

    clusters.sort(key=cluster_rank)
    canonical_cluster = clusters[0]
    canonical_claim = min(
        (c for c in canonical_cluster if _is_table_sourced(c)),
        key=lambda c: c.id,
        default=min(canonical_cluster, key=lambda c: c.id),
    )

    # Same-fact: every OTHER claim in the canonical cluster corroborates it.
    for other in canonical_cluster:
        if other.id == canonical_claim.id:
            continue
        if other.page is not None and other.page == canonical_claim.page:
            # Same page -- E1's within-page reducer already covers this
            # pairing (SIM-341); re-deriving it here would double-count.
            summary.skipped_same_page_pairs += 1
            continue
        await _write_edge(
            session,
            org_id=other.org_id,
            from_claim_id=other.id,
            to_claim_id=canonical_claim.id,
            type_="same_fact",
            basis=f"cross-page reconciliation: {other.attribute} matches the canonical claim",
            run_id=run_id,
            metadata_={"rule": "value_match", "tolerance": _SAME_FACT_REL_TOL},
        )
        summary.same_fact_edges += 1
        if not other.flags or "superseded_by_same_fact" not in other.flags:
            other.flags = [*(other.flags or []), "superseded_by_same_fact"]
            summary.claims_flagged += 1

    # Contradicts: one edge per non-canonical cluster, against the canonical
    # claim -- not a full pairwise mesh across every disagreeing cluster.
    # Keeps the edge count linear in claim count and gives every conflicting
    # value a single, findable edge back to what the pass considered "the"
    # fact, which is what a consumer needs to see the disagreement at all.
    for other_cluster in clusters[1:]:
        rep = min(other_cluster, key=lambda c: c.id)
        if rep.page is not None and rep.page == canonical_claim.page:
            summary.skipped_same_page_pairs += 1
            continue
        from_id, to_id = _canonical_from_to(rep.id, canonical_claim.id)
        value_delta = float(rep.value["normalized"]) - float(canonical_claim.value["normalized"])
        await _write_edge(
            session,
            org_id=rep.org_id,
            from_claim_id=from_id,
            to_claim_id=to_id,
            type_="contradicts",
            basis=f"cross-page reconciliation: {rep.attribute} disagrees with the canonical claim",
            run_id=run_id,
            metadata_={"value_delta": value_delta},
        )
        summary.contradicts_edges += 1


async def _write_edge(
    session: AsyncSession,
    *,
    org_id: int,
    from_claim_id: uuid.UUID,
    to_claim_id: uuid.UUID,
    type_: str,
    basis: str,
    run_id: str,
    metadata_: dict,
) -> None:
    stmt = (
        pg_insert(Edge)
        .values(
            org_id=org_id,
            from_claim_id=from_claim_id,
            to_claim_id=to_claim_id,
            type=type_,
            basis=basis,
            created_by="reconciliation",
            run_id=run_id,
            metadata_=metadata_,
        )
        .on_conflict_do_nothing(constraint="uq_edges_org_from_to_type")
    )
    await session.execute(stmt)
