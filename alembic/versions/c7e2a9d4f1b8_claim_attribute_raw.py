"""Inspector foundation: attribute_raw on claims

Revision ID: c7e2a9d4f1b8
Revises: b4f8e1c3a962
Create Date: 2026-08-26 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c7e2a9d4f1b8"
down_revision: str | Sequence[str] | None = "b4f8e1c3a962"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The parser's pre-canonicalization label (the table path / prose phrase),
    # already in the claims contract and emitted by the parser but dropped at
    # ingest until now. Nullable, no server_default: legacy rows have none and it
    # is not recoverable from a stored row -- a re-ingest backfills it.
    op.add_column("claims", sa.Column("attribute_raw", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("claims", "attribute_raw")
