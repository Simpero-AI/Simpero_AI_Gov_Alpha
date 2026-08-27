"""entity_resolution -- the registry anchor for a deal's company

DS-A-BGCHK-1 (SIM-262). Deal-level, write-once record of one resolution
attempt against one registry: what was searched, what answered, and the anchor
it produced (SEC EDGAR's CIK).

Deliberately NOT a widening of corroboration_events. That table is the right
home for "an outside source agreed/disagreed with THIS claim" and its claim_id
is NOT NULL; "this deal is CIK 0001326801" has no claim behind it. Making
claim_id nullable to fit it is AE-A-CORR-4 (SIM-409)'s decision to make, and
pre-empting it here would weaken an invariant that table's whole append-only
claim-scoped contract rests on.

UPDATE/DELETE are revoked from dd_app: this row is the anchor every downstream
check inherits (SIM-408's harvest, SIM-253's reconcile, SIM-254's roll-up), so
if it were mutable an old memo's citations would silently re-point at a
different filer. Re-resolving INSERTs a new row.

Revision ID: c4f1a9e07b23
Revises: 705aa067992a
Create Date: 2026-08-18 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c4f1a9e07b23"
down_revision: str | Sequence[str] | None = "705aa067992a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entity_resolution",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("deal_id", sa.UUID(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        # The name as searched. deals.name is mutable, so without this a row
        # can only say what the deal is called now, not what produced this
        # answer.
        sa.Column("query_name", sa.Text(), nullable=False),
        sa.Column("registry_id", sa.Text(), nullable=True),
        sa.Column("legal_name", sa.Text(), nullable=True),
        sa.Column("former_names", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("matched_on", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # clock_timestamp(), not now() -- see the model's comment: now() is
        # constant across a transaction, which makes latest_for_deal ambiguous
        # between two resolutions written in one.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organisation.id"]),
        sa.ForeignKeyConstraint(["deal_id"], ["deals.id"]),
        sa.CheckConstraint("source IN ('sec_edgar')", name="ck_entity_resolution_source"),
        sa.CheckConstraint(
            "status IN ('resolved', 'not_found', 'unresolved')",
            name="ck_entity_resolution_status",
        ),
        # Both directions, deliberately. A `resolved` with no anchor is
        # indistinguishable from a guess; an anchor on a `not_found` or
        # `unresolved` would be an answer we explicitly declined to give.
        # Same discipline as claims' ck_claims_checked_requires_method.
        sa.CheckConstraint(
            "(status = 'resolved') = (registry_id IS NOT NULL)",
            name="ck_entity_resolution_resolved_requires_registry_id",
        ),
        sa.CheckConstraint(
            "matched_on IS NULL OR matched_on IN ('current_name', 'former_name')",
            name="ck_entity_resolution_matched_on",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_entity_resolution_org_id"), "entity_resolution", ["org_id"], unique=False
    )
    op.create_index(
        op.f("ix_entity_resolution_deal_id"), "entity_resolution", ["deal_id"], unique=False
    )
    op.create_index(
        op.f("ix_entity_resolution_source"), "entity_resolution", ["source"], unique=False
    )
    op.create_index(
        op.f("ix_entity_resolution_status"), "entity_resolution", ["status"], unique=False
    )
    # The anchor is what SIM-408's harvest looks a deal up BY, so it is the one
    # non-tenant column with a real read pattern behind its index.
    op.create_index(
        op.f("ix_entity_resolution_registry_id"),
        "entity_resolution",
        ["registry_id"],
        unique=False,
    )

    # RLS enabled + FORCEd in the same migration that creates the table, same
    # idiom as screening_result/corroboration_events: a window with the table
    # unprotected is a window where any org can read another org's deal
    # identities, and FORCE closes the table-owner-bypass gap ENABLE alone
    # leaves open.
    op.execute("ALTER TABLE entity_resolution ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY org_isolation ON entity_resolution
            FOR ALL TO dd_app
            USING (org_id IN (
                SELECT id FROM organisation
                WHERE clerk_org_id = current_setting('app.org_id', true)
            ))
    """)
    op.execute("ALTER TABLE entity_resolution FORCE ROW LEVEL SECURITY")

    # Write-once at the database layer. The bootstrap migration's ALTER DEFAULT
    # PRIVILEGES granted dd_app full DML on every table doadmin creates; take
    # UPDATE and DELETE straight back. Nothing in this table is ever
    # legitimately mutated.
    op.execute("REVOKE UPDATE, DELETE ON entity_resolution FROM dd_app")


def downgrade() -> None:
    op.drop_index(op.f("ix_entity_resolution_registry_id"), table_name="entity_resolution")
    op.drop_index(op.f("ix_entity_resolution_status"), table_name="entity_resolution")
    op.drop_index(op.f("ix_entity_resolution_source"), table_name="entity_resolution")
    op.drop_index(op.f("ix_entity_resolution_deal_id"), table_name="entity_resolution")
    op.drop_index(op.f("ix_entity_resolution_org_id"), table_name="entity_resolution")
    op.drop_table("entity_resolution")
