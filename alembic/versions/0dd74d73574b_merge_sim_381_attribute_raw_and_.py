"""merge SIM-381 attribute_raw and analysis run heads

Revision ID: 0dd74d73574b
Revises: 3fd6292e23f0, f3a1c9d8e2b7
Create Date: 2026-08-13 01:54:05.296206

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0dd74d73574b"
# str | Sequence[str] rather than just str: a merge migration's down_revision
# is a tuple of the heads it joins, not a single revision id.
down_revision: str | Sequence[str] | None = ("3fd6292e23f0", "f3a1c9d8e2b7")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
