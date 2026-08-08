"""SIM-366 edges table

Revision ID: e8032cf68796
Revises: b72fe26e2b54
Create Date: 2026-08-04 12:46:44.417363

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e8032cf68796"
# str | Sequence[str] rather than just str: a merge migration's down_revision
# is a tuple of the heads it joins, not a single revision id.
down_revision: str | Sequence[str] | None = "b72fe26e2b54"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "edges",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("from_claim_id", sa.UUID(), nullable=False),
        sa.Column("to_claim_id", sa.UUID(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("basis", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # SIM-366: all four locked edge types now. Only same_fact/contradicts are
        # produced or consumed today; derived_from/supersedes are reserved, schema-
        # only -- the CHECK accepts them, nothing writes or reads one yet. Listed
        # ahead of use because adding a value later is an ALTER on a live RLS table;
        # derived_from lands with the consistency pass (3b).
        sa.CheckConstraint(
            "type IN ('same_fact', 'derived_from', 'contradicts', 'supersedes')",
            name="ck_edges_type",
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organisation.id"]),
        # ON DELETE RESTRICT (not cascade): re-ingest tears edges down explicitly
        # and in order (delete edges -> delete/upsert claims -> re-insert edges).
        sa.ForeignKeyConstraint(["from_claim_id"], ["claims.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["to_claim_id"], ["claims.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_edges_org_id"), "edges", ["org_id"])
    op.create_index(op.f("ix_edges_from_claim_id"), "edges", ["from_claim_id"])
    op.create_index(op.f("ix_edges_to_claim_id"), "edges", ["to_claim_id"])
    # Edges are tenant data, so RLS is enabled HERE, in the table's own migration
    # (same reasoning as chunks/claims): ENABLE ROW LEVEL SECURITY is default-deny,
    # so a window without a policy silently returns zero rows rather than leaking.
    op.execute("ALTER TABLE edges ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY org_isolation ON edges
            FOR ALL TO dd_app
            USING (org_id IN (
                SELECT id FROM organisation
                WHERE clerk_org_id = current_setting('app.org_id', true)
            ))
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS org_isolation ON edges")
    op.drop_index(op.f("ix_edges_to_claim_id"), table_name="edges")
    op.drop_index(op.f("ix_edges_from_claim_id"), table_name="edges")
    op.drop_index(op.f("ix_edges_org_id"), table_name="edges")
    op.drop_table("edges")
