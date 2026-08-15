"""mandates

mandate_categories/mandate_options are global reference/taxonomy tables --
no org_id, no RLS, shared across every tenant. Read-only on the product
portal; full CRUD on the admin portal, platform admins only.

mandates is the org's own investment mandate -- one row per org (unique
org_id), same org_isolation RLS shape as investment_profiles/deals/claims.

Revision ID: a1c3e7f2b4d9
Revises: f3a8c9d2e1b7
Create Date: 2026-08-14 23:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a1c3e7f2b4d9"
down_revision: str | Sequence[str] | None = "f3a8c9d2e1b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mandate_categories",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("category", sa.String(length=150), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("category", name="uq_mandate_categories_category"),
    )
    op.create_index(
        op.f("ix_mandate_categories_category"), "mandate_categories", ["category"], unique=False
    )

    op.create_table(
        "mandate_options",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("category_id", sa.UUID(), nullable=False),
        sa.Column("parent_option_id", sa.UUID(), nullable=True),
        sa.Column("option", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["mandate_categories.id"],
            name=op.f("fk_mandate_options_category_id_mandate_categories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_option_id"],
            ["mandate_options.id"],
            name=op.f("fk_mandate_options_parent_option_id_mandate_options"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_mandate_options_category_id"), "mandate_options", ["category_id"], unique=False
    )
    op.create_index(
        op.f("ix_mandate_options_parent_option_id"),
        "mandate_options",
        ["parent_option_id"],
        unique=False,
    )
    # Two partial indexes, not one (category_id, parent_option_id, option)
    # index -- see the model's docstring. Top-level rows (parent_option_id
    # IS NULL) unique per category; child rows unique per parent.
    op.create_index(
        "uq_mandate_options_category_option",
        "mandate_options",
        ["category_id", "option"],
        unique=True,
        postgresql_where=sa.text("parent_option_id IS NULL"),
    )
    op.create_index(
        "uq_mandate_options_parent_option",
        "mandate_options",
        ["parent_option_id", "option"],
        unique=True,
        postgresql_where=sa.text("parent_option_id IS NOT NULL"),
    )

    op.create_table(
        "mandates",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("mandate", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["org_id"], ["organisation.id"], name=op.f("fk_mandates_org_id_organisation")
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_mandates_user_id_users")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", name="uq_mandates_org_id"),
    )
    op.create_index(op.f("ix_mandates_org_id"), "mandates", ["org_id"], unique=False)
    op.create_index(op.f("ix_mandates_user_id"), "mandates", ["user_id"], unique=False)

    # RLS enabled + policy created in the same migration that creates the
    # table, same idiom as deals/investment_profiles/claims. mandate_categories
    # and mandate_options intentionally get none -- they have no org_id to
    # scope on, by design (global reference data).
    op.execute("ALTER TABLE mandates ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY org_isolation ON mandates
            FOR ALL TO dd_app
            USING (org_id IN (
                SELECT id FROM organisation
                WHERE clerk_org_id = current_setting('app.org_id', true)
            ))
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS org_isolation ON mandates")
    op.drop_index(op.f("ix_mandates_user_id"), table_name="mandates")
    op.drop_index(op.f("ix_mandates_org_id"), table_name="mandates")
    op.drop_table("mandates")

    op.drop_index("uq_mandate_options_parent_option", table_name="mandate_options")
    op.drop_index("uq_mandate_options_category_option", table_name="mandate_options")
    op.drop_index(op.f("ix_mandate_options_parent_option_id"), table_name="mandate_options")
    op.drop_index(op.f("ix_mandate_options_category_id"), table_name="mandate_options")
    op.drop_table("mandate_options")

    op.drop_index(op.f("ix_mandate_categories_category"), table_name="mandate_categories")
    op.drop_table("mandate_categories")
