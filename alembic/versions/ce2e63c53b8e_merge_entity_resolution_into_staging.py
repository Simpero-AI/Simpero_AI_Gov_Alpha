"""merge entity_resolution into staging

Revision ID: ce2e63c53b8e
Revises: 67e5302afcfe, ad2be3d04335
Create Date: 2026-08-27 14:02:29.587426

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "ce2e63c53b8e"
# str | Sequence[str] rather than just str: a merge migration's down_revision
# is a tuple of the heads it joins, not a single revision id.
down_revision: str | Sequence[str] | None = ("67e5302afcfe", "ad2be3d04335")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
