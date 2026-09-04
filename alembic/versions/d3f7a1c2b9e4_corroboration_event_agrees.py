"""corroboration event agrees flag

Persist each outside-source check's agree/disagree judgment on the event row.
Until now `agrees` was only a parameter to record_corroboration_result with a
side effect (a False marks the claim `conflicted`) and was never stored, so the
per-event verdict couldn't be read back for the corroboration display. The
column is nullable: a presence-only source may record a finding without a binary
verdict, and there are no existing rows to backfill in any environment (the
sources are a registered no-op until the corroboration pass runs post-deploy).

Revision ID: d3f7a1c2b9e4
Revises: 1dcfa5bd613d
Create Date: 2026-09-04 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d3f7a1c2b9e4"
down_revision: str | Sequence[str] | None = "1dcfa5bd613d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "corroboration_events",
        sa.Column("agrees", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("corroboration_events", "agrees")
