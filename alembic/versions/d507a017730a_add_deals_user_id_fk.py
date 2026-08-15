"""add deals.user_id fk

Revision ID: d507a017730a
Revises: 3fd6292e23f0
Create Date: 2026-08-14 22:29:13.648522

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d507a017730a"
# str | Sequence[str] rather than just str: a merge migration's down_revision
# is a tuple of the heads it joins, not a single revision id.
down_revision: str | Sequence[str] | None = "3fd6292e23f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("deals", sa.Column("user_id", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_deals_user_id"), "deals", ["user_id"], unique=False)
    op.create_foreign_key(op.f("fk_deals_user_id_users"), "deals", "users", ["user_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint(op.f("fk_deals_user_id_users"), "deals", type_="foreignkey")
    op.drop_index(op.f("ix_deals_user_id"), table_name="deals")
    op.drop_column("deals", "user_id")
