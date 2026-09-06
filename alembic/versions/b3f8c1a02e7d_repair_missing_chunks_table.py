"""repair: create the chunks table where it is recorded-applied but physically absent

On 2026-09-06 the staging DB was found to have `chunks` missing from every schema
(confirmed via pg_tables), even though alembic_version is a descendant of the
chunks-table migration (6c8bc5907f94), so alembic considers it applied and will
never re-create it. The first statement to touch `chunks` (the f4a1c8e2b9d3 GRANT)
hit UndefinedTableError and aborted the deploy. Cause of the drift is unknown
(never physically created, or dropped out-of-band) -- but the fix is the same.

This migration reconstructs the chunks table's FINAL state (6c8bc5907f94's schema
+ the 77be2ddc60a0 document_id FK + its RLS policy/indexes + an explicit dd_app
grant) ONLY when the table is absent. On a clean history (CI, and any environment
where chunks already exists) the guard makes it a pure no-op, so it is safe
everywhere and also repairs prod should it carry the same gap. It does NOT change
the recorded revision of 6c8bc5907f94 -- it just makes the physical schema match
what alembic already believes.

Revision ID: b3f8c1a02e7d
Revises: f4a1c8e2b9d3
Create Date: 2026-09-06 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import TSVECTOR

from alembic import op

revision: str = "b3f8c1a02e7d"
down_revision: str | Sequence[str] | None = "f4a1c8e2b9d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = 1024


def upgrade() -> None:
    # Only act when the physical table is missing. Everywhere it already exists
    # (clean history, CI, a consistent prod), this is a no-op.
    if "chunks" in sa.inspect(op.get_bind()).get_table_names():
        return

    # pgvector is normally enabled far upstream (9e796d5efdb7); guard in case this
    # DB's drift also lost it, so the Vector column below can be created.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Reconstruct the table's FINAL state: 6c8bc5907f94's columns + the
    # 77be2ddc60a0 document_id -> data_source FK (data_source exists by now), so a
    # freshly-repaired table is byte-identical to one built by the normal chain.
    op.create_table(
        "chunks",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column("embedding_version", sa.Text(), nullable=True),
        sa.Column(
            "content_tsv",
            TSVECTOR(),
            sa.Computed("to_tsvector('english', content)", persisted=True),
            nullable=True,
        ),
        sa.Column("element_type", sa.Text(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organisation.id"]),
        sa.ForeignKeyConstraint(
            ["document_id"], ["data_source.id"], name="fk_chunks_document_id_data_source"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_chunks_org_id"), "chunks", ["org_id"], unique=False)
    op.create_index(op.f("ix_chunks_document_id"), "chunks", ["document_id"], unique=False)
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "ix_chunks_content_tsv_gin",
        "chunks",
        ["content_tsv"],
        unique=False,
        postgresql_using="gin",
    )

    op.execute("ALTER TABLE chunks ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY org_isolation ON chunks
            FOR ALL TO dd_app
            USING (org_id IN (
                SELECT id FROM organisation
                WHERE clerk_org_id = current_setting('app.org_id', true)
            ))
    """)
    # Explicit grant: the repaired table is created by the migration role, so make
    # dd_app's SELECT/INSERT explicit rather than relying on the doadmin
    # default-privilege bootstrap firing for whatever role runs migrations here.
    op.execute("GRANT SELECT, INSERT ON chunks TO dd_app")


def downgrade() -> None:
    # No-op: 6c8bc5907f94.downgrade() owns dropping the chunks table. A repair
    # that recreated a missing table must not drop it on downgrade -- that would
    # re-open the very inconsistency this fixes.
    pass
