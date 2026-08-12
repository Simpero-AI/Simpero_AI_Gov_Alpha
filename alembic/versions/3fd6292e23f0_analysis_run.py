"""analysis run

Tracks one "Start Analysis" attempt per deal (docs/plans/start-analysis-flow-alpha.md).
Standard house pattern -- RLS ENABLE+FORCE with the same org_isolation policy
shape as data_source/deals, column-level REVOKE/GRANT narrowing dd_app to the
columns the worker task legitimately mutates -- with one deliberate
deviation from data_source's precedent: **no one-way status trigger here**.
data_source has one because it has exactly one legitimate transition, ever
(pending -> a terminal status). analysis_run genuinely walks
queued -> in_progress -> successful|failed -- a real multi-step lifecycle,
not a single edge -- so a one-way trigger would cargo-cult a constraint
that contradicts the row's actual purpose. Double-submission is prevented
instead by uq_analysis_run_active, a partial unique index on
(deal_id) WHERE status IN ('queued', 'in_progress'): at most one active run
per deal, enforced by Postgres itself rather than an app-level check that
two concurrent requests could both race past. Note this index is NOT scoped
by job_name -- it still enforces one active run per *deal*, full stop, since
only the "parsing" job_name is actually created anywhere in this app today.
If extraction/verification ever start running as their own independent
job_names, revisit whether they should scope uq_analysis_run_active to
(deal_id, job_name) instead.

`job_name` (Text, CHECK'd to 'parsing'/'extraction'/'verification',
server_default 'parsing') names what kind of job this run represents.
Identity, append-only, same as org_id/deal_id -- only "parsing" is wired up
anywhere in this app right now (start_deal_analysis's fan-out); the other
two are named ahead of their own implementation so this table doesn't need
a schema change once they exist.

Revision ID: 3fd6292e23f0
Revises: 92fda2e2a5db
Create Date: 2026-08-10 00:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "3fd6292e23f0"
down_revision: str | Sequence[str] | None = "92fda2e2a5db"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_ANALYSIS_RUN_STATUS = "queued"
DEFAULT_ANALYSIS_RUN_JOB_NAME = "parsing"


def upgrade() -> None:
    op.create_table(
        "analysis_run",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("deal_id", sa.UUID(), nullable=False),
        # Identity, append-only. Only "parsing" is built today -- extraction
        # and verification are named ahead of their own implementation, per
        # Vansh, so this table can host those job types without a schema
        # change once they exist.
        sa.Column(
            "job_name", sa.Text(), server_default=DEFAULT_ANALYSIS_RUN_JOB_NAME, nullable=False
        ),
        sa.Column("selected_frameworks", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.Text(), server_default=DEFAULT_ANALYSIS_RUN_STATUS, nullable=False),
        sa.Column("parse_jobs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        # Nullable, no server_default: stays NULL until the run reaches a
        # terminal status. Frontend-facing summary derived from parse_jobs
        # at that point -- see the model's docstring for the shape.
        sa.Column("job_comments", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Nullable, no server_default: stays NULL until the run reaches a
        # terminal status (successful/failed) -- same idiom as
        # data_source.status_updated_at (set server-side, once, at the
        # moment of the transition it records).
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'in_progress', 'successful', 'failed')",
            name="ck_analysis_run_status",
        ),
        sa.CheckConstraint(
            "job_name IN ('parsing', 'extraction', 'verification')",
            name="ck_analysis_run_job_name",
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organisation.id"]),
        sa.ForeignKeyConstraint(["deal_id"], ["deals.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_analysis_run_org_id"), "analysis_run", ["org_id"], unique=False)
    op.create_index(op.f("ix_analysis_run_deal_id"), "analysis_run", ["deal_id"], unique=False)
    # D6: at most one active (queued/in_progress) run per deal -- the actual
    # double-submit guarantee; the handler's fast-path SELECT is a courtesy
    # for a friendly 409, not the enforcement.
    op.execute("""
        CREATE UNIQUE INDEX uq_analysis_run_active ON analysis_run (deal_id)
            WHERE status IN ('queued', 'in_progress')
    """)

    # RLS enabled + FORCEd in the same migration that creates the table, same
    # idiom as data_source: a window with the table unprotected is a window
    # where any org can read another org's analysis runs, and FORCE closes
    # the table-owner-bypass gap ENABLE alone leaves open.
    op.execute("ALTER TABLE analysis_run ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY org_isolation ON analysis_run
            FOR ALL TO dd_app
            USING (org_id IN (
                SELECT id FROM organisation
                WHERE clerk_org_id = current_setting('app.org_id', true)
            ))
    """)
    op.execute("ALTER TABLE analysis_run FORCE ROW LEVEL SECURITY")

    # The bootstrap migration's ALTER DEFAULT PRIVILEGES already granted
    # dd_app SELECT/INSERT/UPDATE/DELETE on every table doadmin creates.
    # Narrow that down: org_id/deal_id/job_name/selected_frameworks/started_at/id
    # are identity columns and stay append-only; only the columns the worker task
    # (start_deal_analysis) legitimately mutates get UPDATE back. ended_at is
    # one of those -- it's written once, by update_progress, the moment the
    # run reaches a terminal status.
    op.execute("REVOKE UPDATE, DELETE ON analysis_run FROM dd_app")
    op.execute(
        "GRANT UPDATE (status, parse_jobs, error_message, job_comments, ended_at, updated_at) "
        "ON analysis_run TO dd_app"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE analysis_run NO FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS org_isolation ON analysis_run")
    op.execute("DROP INDEX IF EXISTS uq_analysis_run_active")
    op.drop_index(op.f("ix_analysis_run_deal_id"), table_name="analysis_run")
    op.drop_index(op.f("ix_analysis_run_org_id"), table_name="analysis_run")
    op.drop_table("analysis_run")
