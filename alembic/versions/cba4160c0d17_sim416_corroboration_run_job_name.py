"""SIM-416: widen ck_analysis_run_job_name to accept 'corroboration'.

External corroboration becomes its own chained analysis_run stage
(start_deal_corroboration), sitting between verification and screening, so a
`corroboration` run row must be able to exist. The only schema change is the
job_name CHECK: everything else about the stage (progress, RLS, the active-run
index) reuses analysis_run exactly as verification/screening already do.

Mirrors 7c1e4b90d3a2's 'screening' widening: swap the CHECK constraint, and on
downgrade delete the now-illegal rows before narrowing it back (a downgrade that
leaves the constraint uncreatable is not a downgrade). Lossy by necessity -- the
corroboration runs do not survive a downgrade, the honest consequence of
removing the stage that created them. No corroboration-specific table references
analysis_run (CorroborationEvent keys off claim_id, not the run), so deleting
those rows violates no FK.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cba4160c0d17"
down_revision: str | Sequence[str] | None = "1dcfa5bd613d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_JOB_NAMES = "job_name IN ('parsing', 'extraction', 'verification', 'screening')"
_NEW_JOB_NAMES = (
    "job_name IN ('parsing', 'extraction', 'verification', 'screening', 'corroboration')"
)


def upgrade() -> None:
    op.drop_constraint("ck_analysis_run_job_name", "analysis_run", type_="check")
    op.create_check_constraint("ck_analysis_run_job_name", "analysis_run", _NEW_JOB_NAMES)


def downgrade() -> None:
    op.execute("DELETE FROM analysis_run WHERE job_name = 'corroboration'")
    op.drop_constraint("ck_analysis_run_job_name", "analysis_run", type_="check")
    op.create_check_constraint("ck_analysis_run_job_name", "analysis_run", _OLD_JOB_NAMES)
