"""deal_intake_questions

Global reference/taxonomy table (no org_id, no RLS) -- structurally identical
to mandate_categories/mandate_options. Flat ordered list (Q7, the whole
active set applies to every link's questions_snapshot), platform-admin-only
CRUD (Q14, see P2-02).

question_key is immutable once a link has snapshotted it, so uniqueness is
scoped to active rows via a partial index rather than a plain unique column
-- deactivating a question (soft toggle, never a hard delete -- P2-02) frees
its key for reuse without touching rows a past snapshot still points at.

input_type is CHECK-constrained to the currently supported set (review
comment on this PR): a link's questions_snapshot freezes input_type at
generation time, so an invalid value here doesn't just fail on read -- it
gets baked into every link issued while the row is active.

Re-pointed onto d1f3b6e28a94, staging's head as of 2026-08-27 (the P1
intake migrations #125-134 plus the inspector dashboard's #135 all landed
after this branch was last synced). This table is independent of every one
of them, so it stays a plain linear child of whatever staging's head is
rather than a merge revision -- same move as the earlier re-point off
1a2b3c4d5e6f, which two migrations had branched from independently and
left as two heads with nothing joining them.

Revision ID: 67e5302afcfe
Revises: d1f3b6e28a94
Create Date: 2026-08-25 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "67e5302afcfe"
down_revision: str | Sequence[str] | None = "d1f3b6e28a94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deal_intake_questions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("question_key", sa.String(length=100), nullable=False),
        sa.Column("prompt", sa.String(length=500), nullable=False),
        sa.Column("help_text", sa.Text(), nullable=True),
        sa.Column("input_type", sa.String(length=50), nullable=False),
        sa.Column("required", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "input_type IN ('text', 'textarea')", name="ck_deal_intake_questions_input_type"
        ),
    )
    op.create_index(
        "uq_deal_intake_questions_active_key",
        "deal_intake_questions",
        ["question_key"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_index("uq_deal_intake_questions_active_key", table_name="deal_intake_questions")
    op.drop_table("deal_intake_questions")
