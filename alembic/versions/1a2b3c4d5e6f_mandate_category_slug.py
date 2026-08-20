"""mandate category slug

Adds an immutable `slug` column to mandate_categories -- a second, stable
identifier assigned once from a fixed backend-owned enum (see
CreateMandateCategoryRequest.slug), decoupled from the free-text, admin-
editable `category` display name that the product Builder used to join on.
Nullable + partial unique index, same idiom as the mandate_options partial
indexes.

Backfills slug for existing rows by matching `category` (case-insensitively,
trimmed) against the eight canonical display names. Rows that don't match
any of them are left with slug NULL rather than guessed.

Revision ID: 1a2b3c4d5e6f
Revises: 705aa067992a
Create Date: 2026-08-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "1a2b3c4d5e6f"
down_revision: str | Sequence[str] | None = "705aa067992a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Canonical name -> slug, the same eight fixed slots the frontend Builder
# renders UI for (Simpero_AI_Gov_Web src/lib/mandateSelection.ts).
CANONICAL_SLUGS = {
    "investment stage": "investment_stage",
    "geographies": "geographies",
    "target sectors": "target_sectors",
    "deal types": "deal_types",
    "asset classes": "asset_classes",
    "must have": "must_have",
    "deal breaker": "deal_breaker",
    "check size range": "check_size_range",
}


def upgrade() -> None:
    op.add_column("mandate_categories", sa.Column("slug", sa.String(length=50), nullable=True))
    op.create_index(
        "uq_mandate_categories_slug",
        "mandate_categories",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("slug IS NOT NULL"),
    )

    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, category FROM mandate_categories")).fetchall()
    for row_id, category in rows:
        slug = CANONICAL_SLUGS.get(category.strip().lower())
        if slug is None:
            print(f"[mandate_categories slug backfill] no match, leaving NULL: {category!r}")
            continue
        print(f"[mandate_categories slug backfill] {category!r} -> {slug}")
        conn.execute(
            sa.text("UPDATE mandate_categories SET slug = :slug WHERE id = :id"),
            {"slug": slug, "id": row_id},
        )


def downgrade() -> None:
    op.drop_index("uq_mandate_categories_slug", table_name="mandate_categories")
    op.drop_column("mandate_categories", "slug")
