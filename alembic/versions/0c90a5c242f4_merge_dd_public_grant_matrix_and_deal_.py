"""merge dd_public grant matrix and deal qualitative findings heads

Revision ID: 0c90a5c242f4
Revises: 8f2a4c6e9b31, 9e0d7c1a4b52
Create Date: 2026-08-26 10:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0c90a5c242f4"
down_revision: str | Sequence[str] | None = ("8f2a4c6e9b31", "9e0d7c1a4b52")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
