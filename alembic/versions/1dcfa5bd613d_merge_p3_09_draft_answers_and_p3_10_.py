"""merge P3-09 draft_answers and P3-10 intake_link_id heads

Revision ID: 1dcfa5bd613d
Revises: 2f7e83611f52, 9a48cce5ecac
Create Date: 2026-08-28 18:32:27.941427

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "1dcfa5bd613d"
# str | Sequence[str] rather than just str: a merge migration's down_revision
# is a tuple of the heads it joins, not a single revision id.
down_revision: str | Sequence[str] | None = ("2f7e83611f52", "9a48cce5ecac")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
