"""claims deal_id not null

deal_id already carries a clean FK to deals.id (0 orphaned rows, added in
77be2ddc60a0_data_source_fks.py) but was still nullable. Making it required:
9 existing rows have deal_id IS NULL, all pre-FK test data (org_id=3,
entity='Test Entity', attribute='test_attr', status='missing', created
2026-07-26/28) -- deleted here before the NOT NULL is applied.

Revision ID: f3a8c9d2e1b7
Revises: d507a017730a
Create Date: 2026-08-14 23:10:00.000000

"""

from collections.abc import Sequence

from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f3a8c9d2e1b7"
down_revision: str | Sequence[str] | None = "d507a017730a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM claims WHERE deal_id IS NULL")
    op.alter_column(
        "claims", "deal_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False
    )


def downgrade() -> None:
    op.alter_column("claims", "deal_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
