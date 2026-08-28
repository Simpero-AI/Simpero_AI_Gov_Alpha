"""deal intake link draft answers

External Deal Intake Link (P3-09): adds `draft_answers` JSONB to
deal_intake_link -- the read-merge-write target for
POST /api/public/intake/answers (see docs/plans/
external-deal-intake-link-phase-3.md's P3-09 ticket). Additive only: does
not touch 3d7b1f5a8c94's original narrow `GRANT UPDATE (status,
submitted_at, failed_attempts, last_attempt_at) ... TO dd_public` statement.

dd_public already holds whole-table SELECT on deal_intake_link (P1-01), so
the new column's SELECT is automatically visible -- only a new, separate
UPDATE grant is needed. dd_app deliberately does NOT get draft_answers in
its UPDATE grant -- the org side has no legitimate write path to the
external party's in-progress draft; dd_app's existing default SELECT
already covers the new column for free.

Revision ID: 2f7e83611f52
Revises: 76a165315331
Create Date: 2026-08-28 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "2f7e83611f52"
down_revision: str | Sequence[str] | None = "76a165315331"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deal_intake_link",
        sa.Column("draft_answers", postgresql.JSONB(), nullable=True),
    )
    op.execute("GRANT UPDATE (draft_answers) ON deal_intake_link TO dd_public")


def downgrade() -> None:
    op.execute("REVOKE UPDATE (draft_answers) ON deal_intake_link FROM dd_public")
    op.drop_column("deal_intake_link", "draft_answers")
