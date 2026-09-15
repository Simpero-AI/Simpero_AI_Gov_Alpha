"""synthesis_snapshot -- frozen field-synthesis per analysis run

W3 of the consistency effort (reglens-consistency-plan). The grounded
field-synthesis for the Company + Summary narrative sections is computed once
per analysis run and stored here, so GET /deals/{id}/company-synthesis becomes a
pure reader instead of re-running the LLM + retrieval on every page load. Two
loads of the same deal are then byte-identical, and a re-analysis INSERTs a new
row (`latest_for_deal` supersession) rather than re-rolling at request time.

Write-once at the database layer (REVOKE UPDATE, DELETE) -- same idiom as
screening_result: the snapshot is a provenance record of what the analysis
actually produced, and supersession (insert-new, read-latest) keeps history
while avoiding a destructive delete-then-insert (the trap corroboration_events
already demonstrated).

No analysis_run job_name change: the synthesis stage is run-row-less (it holds
no AnalysisRun row), so nothing here touches ck_analysis_run_job_name.

Revision ID: b3f9c7d21a08
Revises: a7d2f4e91c30
Create Date: 2026-09-15 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b3f9c7d21a08"
down_revision: str | Sequence[str] | None = "a7d2f4e91c30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept verbatim-in-sync with SynthesisSnapshot.REASONS (house convention).
_REASONS = "reason IN ('ok', 'no_api_key', 'no_documents', 'no_sections_grounded')"


def upgrade() -> None:
    op.create_table(
        "synthesis_snapshot",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("deal_id", sa.UUID(), nullable=False),
        sa.Column("analysis_run_id", sa.UUID(), nullable=True),
        sa.Column("sections", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        # clock_timestamp(), not now() -- see the model's comment: now() is
        # constant across a transaction, which makes SynthesisSnapshotRepo
        # .latest_for_deal ambiguous between two snapshots written in one.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organisation.id"]),
        sa.ForeignKeyConstraint(["deal_id"], ["deals.id"]),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_run.id"]),
        sa.CheckConstraint(_REASONS, name="ck_synthesis_snapshot_reason"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_synthesis_snapshot_org_id"), "synthesis_snapshot", ["org_id"], unique=False
    )
    op.create_index(
        op.f("ix_synthesis_snapshot_deal_id"), "synthesis_snapshot", ["deal_id"], unique=False
    )
    op.create_index(
        op.f("ix_synthesis_snapshot_analysis_run_id"),
        "synthesis_snapshot",
        ["analysis_run_id"],
        unique=False,
    )

    # RLS enabled + FORCEd in the same migration that creates the table, same
    # idiom as analysis_run/screening_result: a window with the table
    # unprotected is a window where any org can read another org's synthesized
    # summaries, and FORCE closes the table-owner-bypass gap ENABLE alone leaves.
    op.execute("ALTER TABLE synthesis_snapshot ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY org_isolation ON synthesis_snapshot
            FOR ALL TO dd_app
            USING (org_id IN (
                SELECT id FROM organisation
                WHERE clerk_org_id = current_setting('app.org_id', true)
            ))
    """)
    op.execute("ALTER TABLE synthesis_snapshot FORCE ROW LEVEL SECURITY")

    # Write-once at the database layer. The bootstrap migration granted dd_app
    # full DML; take UPDATE and DELETE straight back. Re-analysis INSERTs a new
    # row and the reader picks the latest -- nothing here is ever mutated.
    op.execute("REVOKE UPDATE, DELETE ON synthesis_snapshot FROM dd_app")


def downgrade() -> None:
    op.drop_index(op.f("ix_synthesis_snapshot_analysis_run_id"), table_name="synthesis_snapshot")
    op.drop_index(op.f("ix_synthesis_snapshot_deal_id"), table_name="synthesis_snapshot")
    op.drop_index(op.f("ix_synthesis_snapshot_org_id"), table_name="synthesis_snapshot")
    op.drop_table("synthesis_snapshot")
