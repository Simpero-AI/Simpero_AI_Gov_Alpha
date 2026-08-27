"""Path B: qualitative_findings on deals

Revision ID: 9e0d7c1a4b52
Revises: 1a2b3c4d5e6f
Create Date: 2026-08-25 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "9e0d7c1a4b52"
down_revision: str | Sequence[str] | None = "1a2b3c4d5e6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The parser's grounded verdict per qualitative (llm) screening rule,
    # {rule_id: {"verdict": ..., "evidence": ...}}. Nullable, no server_default:
    # legacy deals and deals whose mandate selected no llm rule simply have none,
    # and the document evaluators read that as unknown.
    op.add_column(
        "deals",
        sa.Column("qualitative_findings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("deals", "qualitative_findings")
