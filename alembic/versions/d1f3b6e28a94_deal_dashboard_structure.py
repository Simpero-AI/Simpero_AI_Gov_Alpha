"""Inspector: dashboard_structure on deals

Revision ID: d1f3b6e28a94
Revises: c7e2a9d4f1b8
Create Date: 2026-08-26 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d1f3b6e28a94"
down_revision: str | Sequence[str] | None = "c7e2a9d4f1b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The parser's grounded organizing pass -- how to fold this deal's noisy
    # free-text entities into a few business subjects and which metrics lead its
    # story. Nullable, no server_default: legacy deals have none (the Inspector
    # falls back to deterministic frequency grouping) and it is produced at
    # extraction, so a re-ingest backfills it.
    op.add_column("deals", sa.Column("dashboard_structure", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("deals", "dashboard_structure")
