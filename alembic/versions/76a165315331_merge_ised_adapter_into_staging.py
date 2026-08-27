"""merge ised adapter into staging

Revision ID: 76a165315331
Revises: 94493d9f6862, 965c3096f497
Create Date: 2026-08-27 14:44:08.070578

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "76a165315331"
# str | Sequence[str] rather than just str: a merge migration's down_revision
# is a tuple of the heads it joins, not a single revision id.
down_revision: str | Sequence[str] | None = ("94493d9f6862", "965c3096f497")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
