"""SIM-369: edges created_by/run_id/metadata, UNIQUE, no-self-edge

Revision ID: 7b837e251134
Revises: e8032cf68796
Create Date: 2026-08-05 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "7b837e251134"
down_revision: str | Sequence[str] | None = "e8032cf68796"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NOT NULL with a temporary DEFAULT so the ALTER succeeds against any
    # existing rows -- every edge in this table today was written by the
    # parser's E1 reducer via scripts/ingest_claims.py (SIM-366's only writer),
    # so 'extraction_reducer' is the true value for all of them, not a guess.
    # The default is dropped immediately after: every INSERT from here on must
    # name its writer explicitly (see app/models/edge.py's created_by column,
    # which carries no server_default).
    op.add_column(
        "edges",
        sa.Column("created_by", sa.Text(), nullable=False, server_default="extraction_reducer"),
    )
    op.alter_column("edges", "created_by", server_default=None)
    op.add_column("edges", sa.Column("run_id", sa.Text(), nullable=True))
    op.add_column("edges", sa.Column("metadata", postgresql.JSONB(), nullable=True))

    op.create_check_constraint(
        "ck_edges_created_by",
        "edges",
        "created_by IN ('extraction_reducer', 'reconciliation', 'consistency', 'human')",
    )
    # A claim cannot relate to itself; the parser already guards a self-
    # contradicts, this makes it DB-enforced for backend-authored edges too
    # (reconciliation/consistency don't go through the parser's guard).
    op.create_check_constraint("ck_edges_no_self_edge", "edges", "from_claim_id <> to_claim_id")
    # Idempotent edge writes: a re-run of any pass over the same document must
    # not duplicate an edge. Pairs with SIM-367's ordered re-ingest teardown.
    op.create_unique_constraint(
        "uq_edges_org_from_to_type", "edges", ["org_id", "from_claim_id", "to_claim_id", "type"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_edges_org_from_to_type", "edges", type_="unique")
    op.drop_constraint("ck_edges_no_self_edge", "edges", type_="check")
    op.drop_constraint("ck_edges_created_by", "edges", type_="check")
    op.drop_column("edges", "metadata")
    op.drop_column("edges", "run_id")
    op.drop_column("edges", "created_by")
