"""resolved_entity -- the deal's consolidated corroboration anchor

AE-A-CORR (SIM-420). One row per fold: the deal's canonical legal name, its
alias/former-name list, and the registry cross-refs (SEC CIK, ISED
corporationId, BC registration number) that corroboration adapters key on
instead of raw `claim.entity`.

Distinct from entity_resolution (SIM-262) on purpose. That table logs one
ATTEMPT per (deal, registry) and must keep recording `not_found` answers --
"we looked and SEC has nothing" is a real, valuable row. This table is the
folded ANSWER an adapter reads: it exists only when at least one registry
actually resolved, which is what makes "no row" an unambiguous no-signal
rather than something an adapter has to interpret.

UPDATE/DELETE are revoked from dd_app: every corroboration event recorded
against this deal inherits this identity, so a mutable row would silently
re-point old events at a different company. Re-resolving INSERTs a new row.

Revision ID: b7e41d92c5a8
Revises: ad2be3d04335
Create Date: 2026-08-28 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b7e41d92c5a8"
down_revision: str | Sequence[str] | None = "ad2be3d04335"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "resolved_entity",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("deal_id", sa.UUID(), nullable=False),
        sa.Column("canonical_name", sa.Text(), nullable=False),
        # ["Old Name Ltd", ...] -- former/alternate legal names, canonical
        # excluded.
        sa.Column("aliases", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # {"cik": "0000320193", "ised_corporation_id": "1234567", ...}
        sa.Column("registry_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # clock_timestamp(), not now() -- see the model: now() is constant
        # across a transaction, which makes latest_for_deal ambiguous between
        # two folds written in one.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organisation.id"]),
        sa.ForeignKeyConstraint(["deal_id"], ["deals.id"]),
        sa.CheckConstraint("length(btrim(canonical_name)) > 0", name="ck_resolved_entity_has_name"),
        sa.CheckConstraint(
            "jsonb_typeof(aliases) = 'array'", name="ck_resolved_entity_aliases_array"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(registry_ids) = 'object'", name="ck_resolved_entity_registry_ids_object"
        ),
        # A fold with no registry id resolved nothing. It must leave NO row
        # rather than a name-only one an adapter could mistake for a real
        # anchor -- that is what keeps "no resolved entity" an unambiguous
        # no-signal instead of a half-answer. Same discipline as
        # ck_entity_resolution_resolved_requires_registry_id.
        sa.CheckConstraint(
            "registry_ids <> '{}'::jsonb", name="ck_resolved_entity_has_registry_id"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_resolved_entity_org_id"), "resolved_entity", ["org_id"], unique=False)
    op.create_index(
        op.f("ix_resolved_entity_deal_id"), "resolved_entity", ["deal_id"], unique=False
    )

    # RLS enabled + FORCEd in the same migration that creates the table, same
    # idiom as entity_resolution/screening_result/corroboration_events: a
    # window with the table unprotected is a window where any org can read
    # another org's deal identities, and FORCE closes the table-owner-bypass
    # gap ENABLE alone leaves open.
    op.execute("ALTER TABLE resolved_entity ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY org_isolation ON resolved_entity
            FOR ALL TO dd_app
            USING (org_id IN (
                SELECT id FROM organisation
                WHERE clerk_org_id = current_setting('app.org_id', true)
            ))
    """)
    op.execute("ALTER TABLE resolved_entity FORCE ROW LEVEL SECURITY")

    # Write-once at the database layer. The bootstrap migration's ALTER DEFAULT
    # PRIVILEGES granted dd_app full DML on every table doadmin creates; take
    # UPDATE and DELETE straight back.
    op.execute("REVOKE UPDATE, DELETE ON resolved_entity FROM dd_app")


def downgrade() -> None:
    op.drop_index(op.f("ix_resolved_entity_deal_id"), table_name="resolved_entity")
    op.drop_index(op.f("ix_resolved_entity_org_id"), table_name="resolved_entity")
    op.drop_table("resolved_entity")
