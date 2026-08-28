"""merge entity_resolution and mandate_category_slug heads

Revision ID: ad2be3d04335
Revises: 1a2b3c4d5e6f, c4f1a9e07b23
Create Date: 2026-08-22 00:28:13.838648

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "ad2be3d04335"
# str | Sequence[str] rather than just str: a merge migration's down_revision
# is a tuple of the heads it joins, not a single revision id.
down_revision: str | Sequence[str] | None = ("1a2b3c4d5e6f", "c4f1a9e07b23")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
