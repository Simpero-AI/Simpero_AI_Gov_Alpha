"""screening_result + 'screening' as an analysis_run job_name

Screening #4 (SIM-404). Two changes that belong in one migration because
neither is usable without the other:

1. `screening_result` -- the write-once record of one screening pass over a
   deal: the recommendation, the rulebook version that produced it, and the
   per-rule verdicts with their evidence. UPDATE/DELETE are revoked from
   dd_app: this is the record of WHY a deal was auto-declined and under which
   rules, so it must stay answerable after any later re-run. Re-screening
   INSERTs a new row.

2. `ck_analysis_run_job_name` widened to accept 'screening'. The original
   CHECK listed 'parsing'/'extraction'/'verification' only, so the screening
   stage cannot create its run row until this lands.

Note on uq_analysis_run_active (unchanged here, deliberately): it is a
partial unique index on deal_id alone, NOT (deal_id, job_name), so it still
permits only one active run per deal across all job names. The screening
stage therefore has to create its row in the same transaction that marks the
verification run terminal -- see start_deal_screening.py. Widening that index
to (deal_id, job_name) is the change 3fd6292e23f0's own docstring anticipates
"if more job_names appear", but it would also weaken the double-submit
guarantee, so it stays as-is until something actually needs concurrent runs.

Revision ID: 7c1e4b90d3a2
Revises: 565cc5a7589e
Create Date: 2026-08-14 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "7c1e4b90d3a2"
down_revision: str | Sequence[str] | None = "565cc5a7589e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_JOB_NAMES = "job_name IN ('parsing', 'extraction', 'verification')"
_NEW_JOB_NAMES = "job_name IN ('parsing', 'extraction', 'verification', 'screening')"


def upgrade() -> None:
    op.create_table(
        "screening_result",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("deal_id", sa.UUID(), nullable=False),
        sa.Column("analysis_run_id", sa.UUID(), nullable=True),
        sa.Column("rulebook_version", sa.Text(), nullable=False),
        sa.Column("recommendation", sa.Text(), nullable=False),
        sa.Column("rule_results", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # clock_timestamp(), not now() -- see the model's comment: now() is
        # constant across a transaction, which makes ScreeningResultRepo
        # .latest_for_deal ambiguous between two screenings written in one.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organisation.id"]),
        sa.ForeignKeyConstraint(["deal_id"], ["deals.id"]),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_run.id"]),
        sa.CheckConstraint(
            "recommendation IN ('auto_decline', 'green', 'human_review')",
            name="ck_screening_result_recommendation",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_screening_result_org_id"), "screening_result", ["org_id"], unique=False
    )
    op.create_index(
        op.f("ix_screening_result_deal_id"), "screening_result", ["deal_id"], unique=False
    )
    op.create_index(
        op.f("ix_screening_result_analysis_run_id"),
        "screening_result",
        ["analysis_run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_screening_result_recommendation"),
        "screening_result",
        ["recommendation"],
        unique=False,
    )

    # RLS enabled + FORCEd in the same migration that creates the table, same
    # idiom as analysis_run/data_source: a window with the table unprotected
    # is a window where any org can read another org's screening verdicts,
    # and FORCE closes the table-owner-bypass gap ENABLE alone leaves open.
    op.execute("ALTER TABLE screening_result ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY org_isolation ON screening_result
            FOR ALL TO dd_app
            USING (org_id IN (
                SELECT id FROM organisation
                WHERE clerk_org_id = current_setting('app.org_id', true)
            ))
    """)
    op.execute("ALTER TABLE screening_result FORCE ROW LEVEL SECURITY")

    # Write-once at the database layer. The bootstrap migration's ALTER
    # DEFAULT PRIVILEGES granted dd_app full DML on every table doadmin
    # creates; take UPDATE and DELETE straight back. No column-level GRANT
    # follows (unlike analysis_run's, which needs UPDATE on its progress
    # columns) -- nothing in this table is ever legitimately mutated.
    op.execute("REVOKE UPDATE, DELETE ON screening_result FROM dd_app")

    # Widen the job_name CHECK so a 'screening' run row can exist.
    op.drop_constraint("ck_analysis_run_job_name", "analysis_run", type_="check")
    op.create_check_constraint("ck_analysis_run_job_name", "analysis_run", _NEW_JOB_NAMES)


def downgrade() -> None:
    # Order matters: screening_result.analysis_run_id is an FK onto
    # analysis_run, so the table goes FIRST. Deleting the 'screening' runs
    # while any screening_result still references them violates that FK and
    # the downgrade fails halfway.
    op.drop_index(op.f("ix_screening_result_recommendation"), table_name="screening_result")
    op.drop_index(op.f("ix_screening_result_analysis_run_id"), table_name="screening_result")
    op.drop_index(op.f("ix_screening_result_deal_id"), table_name="screening_result")
    op.drop_index(op.f("ix_screening_result_org_id"), table_name="screening_result")
    op.drop_table("screening_result")

    # Now narrow the CHECK back. Any existing 'screening' rows would violate
    # it, so they go next -- a downgrade that leaves the constraint
    # uncreatable is not a downgrade. This is lossy by necessity: the
    # screening runs and their results do not survive a downgrade, which is
    # the honest consequence of removing the feature that created them.
    op.execute("DELETE FROM analysis_run WHERE job_name = 'screening'")
    op.drop_constraint("ck_analysis_run_job_name", "analysis_run", type_="check")
    op.create_check_constraint("ck_analysis_run_job_name", "analysis_run", _OLD_JOB_NAMES)
