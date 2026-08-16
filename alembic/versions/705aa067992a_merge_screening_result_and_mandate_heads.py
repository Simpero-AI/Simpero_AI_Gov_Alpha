"""merge screening_result and mandate heads

Revision ID: 705aa067992a
Revises: 7c1e4b90d3a2, a1c3e7f2b4d9
Create Date: 2026-08-16 11:14:30.210768

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "705aa067992a"
# str | Sequence[str] rather than just str: a merge migration's down_revision
# is a tuple of the heads it joins, not a single revision id.
down_revision: str | Sequence[str] | None = ("7c1e4b90d3a2", "a1c3e7f2b4d9")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
