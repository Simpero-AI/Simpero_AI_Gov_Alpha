"""merge resolved_entity into staging

Revision ID: 94493d9f6862
Revises: ce2e63c53b8e, b7e41d92c5a8
Create Date: 2026-08-27 14:17:43.972019

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "94493d9f6862"
# str | Sequence[str] rather than just str: a merge migration's down_revision
# is a tuple of the heads it joins, not a single revision id.
down_revision: str | Sequence[str] | None = ("ce2e63c53b8e", "b7e41d92c5a8")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
