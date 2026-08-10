"""SIM-381: attribute_raw column on claims

Revision ID: f3a1c9d8e2b7
Revises: 7b837e251134
Create Date: 2026-08-11 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f3a1c9d8e2b7"
down_revision: str | Sequence[str] | None = "7b837e251134"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, no server_default: the contract's attribute_raw is null for any
    # claim canonicalization (SIM-344) never reached (qualitative, or
    # pre-canonicalization), so every row written before this column existed
    # stays valid as-is.
    op.add_column("claims", sa.Column("attribute_raw", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("claims", "attribute_raw")
