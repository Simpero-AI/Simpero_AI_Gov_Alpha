"""Shared bulk edge writer for the reconciliation (SIM-371) and consistency
(SIM-372) passes.

Both passes stage claim-to-claim edges and previously wrote them one INSERT at
a time. On a large deal that is thousands of sequential DB round-trips -- a few
tenths of a millisecond each against a local Postgres, but tens of milliseconds
each against a managed Postgres behind PgBouncer. Same code, same edges: the
work that finished in seconds locally took many minutes on staging purely
because each edge was its own round-trip. Staging the edges and flushing them in
a few bulk INSERTs removes the round-trip-per-edge cost without changing what
gets written.
"""

from __future__ import annotations

import uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Edge

# ~8 bound params per row, so 500 rows keeps each statement well under
# Postgres' 65535 bind-parameter limit while collapsing thousands of
# round-trips into a handful.
_EDGE_INSERT_CHUNK = 500


def stage_edge(
    edges: list[dict],
    *,
    org_id: int,
    from_claim_id: uuid.UUID,
    to_claim_id: uuid.UUID,
    type_: str,
    basis: str,
    created_by: str,
    run_id: str,
    metadata_: dict | None,
) -> None:
    """Accumulate one edge row; the write happens later in flush_edges. Keys
    match Edge's mapped attributes so flush_edges can bulk-insert them as-is."""
    edges.append(
        {
            "org_id": org_id,
            "from_claim_id": from_claim_id,
            "to_claim_id": to_claim_id,
            "type": type_,
            "basis": basis,
            "created_by": created_by,
            "run_id": run_id,
            "metadata_": metadata_,
        }
    )


async def flush_edges(session: AsyncSession, edges: list[dict]) -> None:
    """Write all staged edges in chunked bulk INSERTs, ON CONFLICT DO NOTHING
    against the same UNIQUE(org_id, from, to, type) the per-edge path used --
    identical write semantics, far fewer round-trips.

    Dedupe within the batch first: ON CONFLICT resolves a row against already
    committed rows, not against another row earlier in the same statement, so a
    duplicate staged twice in one run must be collapsed here (the per-edge path
    got this for free, each INSERT being its own statement)."""
    if not edges:
        return
    seen: set[tuple[int, uuid.UUID, uuid.UUID, str]] = set()
    unique: list[dict] = []
    for row in edges:
        key = (row["org_id"], row["from_claim_id"], row["to_claim_id"], row["type"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    for start in range(0, len(unique), _EDGE_INSERT_CHUNK):
        stmt = (
            pg_insert(Edge)
            .values(unique[start : start + _EDGE_INSERT_CHUNK])
            .on_conflict_do_nothing(constraint="uq_edges_org_from_to_type")
        )
        await session.execute(stmt)
