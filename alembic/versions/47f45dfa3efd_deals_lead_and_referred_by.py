"""deals.lead_user_id + deals.referred_by

Revision ID: 47f45dfa3efd
Revises: 1a2b3c4d5e6f
Create Date: 2026-08-23 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "47f45dfa3efd"
down_revision: str | Sequence[str] | None = "1a2b3c4d5e6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("deals", sa.Column("lead_user_id", sa.Integer(), nullable=True))
    op.add_column("deals", sa.Column("referred_by", sa.Text(), nullable=True))
    op.create_index(op.f("ix_deals_lead_user_id"), "deals", ["lead_user_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_deals_lead_user_id_users"), "deals", "users", ["lead_user_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_deals_lead_user_id_users"), "deals", type_="foreignkey")
    op.drop_index(op.f("ix_deals_lead_user_id"), table_name="deals")
    op.drop_column("deals", "referred_by")
    op.drop_column("deals", "lead_user_id")
