"""Screening #3: sector, hq_geography, founder_equity_post_close_pct on deals

Revision ID: 565cc5a7589e
Revises: 3fd6292e23f0
Create Date: 2026-08-13 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "565cc5a7589e"
down_revision: str | Sequence[str] | None = "3fd6292e23f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # All three nullable, no server_default: legacy deals have none of these
    # set (db_04/gs_07/gs_08 return `unknown` until a human fills them in via
    # PATCH /deals/{id}), and founder_equity_post_close_pct in particular is
    # typically decided during deal structuring, after intake, not at
    # creation -- so every existing row stays valid as NULL.
    op.add_column("deals", sa.Column("sector", sa.Text(), nullable=True))
    op.add_column("deals", sa.Column("hq_geography", sa.Text(), nullable=True))
    op.add_column(
        "deals",
        sa.Column("founder_equity_post_close_pct", sa.Numeric(5, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("deals", "founder_equity_post_close_pct")
    op.drop_column("deals", "hq_geography")
    op.drop_column("deals", "sector")
