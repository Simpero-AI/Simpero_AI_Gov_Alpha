"""deal intake link

External Deal Intake Link (P1-01): deal_intake_link, the tenant-scoped table
backing the external intake link (see docs/plans/
external-deal-intake-link-implementation-brief.md section 2.2).

Not audit-log-shaped like human_audit_log -- deal_intake_link is a resource
with a lifecycle (closer to data_source), so it does not get a blanket
REVOKE UPDATE/DELETE. Instead:

- Identity/history columns (org_id, clerk_org_id, deal_id, token_hash,
  recipient_email, questions_snapshot, expires_at, created_by_user_id,
  created_at, id) are append-only: REVOKE UPDATE, DELETE ... FROM dd_app.
- The four lifecycle columns (status, submitted_at, failed_attempts,
  last_attempt_at) get a narrow GRANT UPDATE (...) back -- the legitimate
  writes the intake flow performs (stamping a failed attempt, or flipping
  status to a terminal value).
- A BEFORE UPDATE trigger (deal_intake_link_enforce_one_way_status) makes the
  pending -> terminal transition truly one-way, even against the table owner
  (doadmin) -- same reasoning as data_source_enforce_one_way_status: triggers
  fire for every role, unlike GRANT/REVOKE, which the owning role bypasses.

dd_public gets SELECT + the same narrow UPDATE grant here (no RLS policy for
it yet -- ENABLE + FORCE RLS with a grant but no matching policy is
default-deny, same idiom this codebase already relies on elsewhere). P1-03
adds the dd_public keyhole policies (intake_token_lookup /
intake_session_lookup) in a later migration.

Revision ID: 3d7b1f5a8c94
Revises: 8f2a4c6e9b31
Create Date: 2026-08-25 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "3d7b1f5a8c94"
down_revision: str | Sequence[str] | None = "8f2a4c6e9b31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deal_intake_link",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("clerk_org_id", sa.String(length=64), nullable=False),
        sa.Column("deal_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("recipient_email", sa.String(length=255), nullable=False),
        sa.Column("questions_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'submitted', 'revoked', 'expired')",
            name="ck_deal_intake_link_status",
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organisation.id"]),
        sa.ForeignKeyConstraint(["deal_id"], ["deals.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_deal_intake_link_token_hash"),
    )
    op.create_index(
        op.f("ix_deal_intake_link_org_id"), "deal_intake_link", ["org_id"], unique=False
    )
    op.create_index(
        op.f("ix_deal_intake_link_deal_id"), "deal_intake_link", ["deal_id"], unique=False
    )
    op.create_index(
        op.f("ix_deal_intake_link_status"), "deal_intake_link", ["status"], unique=False
    )
    op.create_index(
        op.f("ix_deal_intake_link_token_hash"), "deal_intake_link", ["token_hash"], unique=False
    )
    op.create_index(
        op.f("ix_deal_intake_link_created_by_user_id"),
        "deal_intake_link",
        ["created_by_user_id"],
        unique=False,
    )

    # One live link per deal -- a second pending link for the same deal
    # cannot be created until the first moves to a terminal status (the
    # lazy-expire-on-generate step in P3 is what keeps this from blocking a
    # reissue; not this migration's concern).
    op.execute(
        "CREATE UNIQUE INDEX ux_deal_intake_link_pending_deal "
        "ON deal_intake_link (deal_id) WHERE status = 'pending'"
    )

    # Tenant data, so RLS is enabled HERE -- in the same migration that
    # creates the table. Same reasoning as data_source: a window with the
    # table unprotected is a window where any org can read another org's
    # intake links/tokens/recipient emails.
    op.execute("ALTER TABLE deal_intake_link ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY org_isolation ON deal_intake_link
            FOR ALL TO dd_app
            USING (org_id IN (
                SELECT id FROM organisation
                WHERE clerk_org_id = current_setting('app.org_id', true)
            ))
    """)
    # FORCE, on top of ENABLE: a plain ENABLE exempts the table owner
    # (doadmin) from its own RLS policy -- same rationale as data_source.
    op.execute("ALTER TABLE deal_intake_link FORCE ROW LEVEL SECURITY")

    # The bootstrap migration's ALTER DEFAULT PRIVILEGES already granted
    # dd_app SELECT/INSERT/UPDATE/DELETE on every table doadmin creates.
    # Narrow that back down: everything except the four lifecycle columns is
    # append-only.
    op.execute("REVOKE UPDATE, DELETE ON deal_intake_link FROM dd_app")
    op.execute(
        "GRANT UPDATE (status, submitted_at, failed_attempts, last_attempt_at) "
        "ON deal_intake_link TO dd_app"
    )

    # dd_public: privilege grant ONLY, no RLS policy yet -- P1-03 adds the
    # keyhole policies (intake_token_lookup / intake_session_lookup) later.
    # ENABLE+FORCE RLS with a grant but no matching policy is default-deny,
    # same idiom this codebase already relies on elsewhere (e.g. a table
    # with RLS enabled before its first policy migration lands).
    op.execute(
        "GRANT SELECT, UPDATE (status, submitted_at, failed_attempts, last_attempt_at) "
        "ON deal_intake_link TO dd_public"
    )

    # The part that makes the one-way status transition airtight: a BEFORE
    # UPDATE trigger enforcing it, fired for EVERY role, including the table
    # owner (doadmin) -- unlike GRANT/REVOKE, which the owner bypasses by
    # virtue of owning the table.
    op.execute("""
        CREATE FUNCTION deal_intake_link_enforce_one_way_status() RETURNS trigger AS $$
        BEGIN
            IF OLD.status <> 'pending' THEN
                RAISE EXCEPTION 'deal_intake_link % status is final once left pending (was %)',
                    OLD.id, OLD.status;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_deal_intake_link_one_way_status
            BEFORE UPDATE ON deal_intake_link
            FOR EACH ROW EXECUTE FUNCTION deal_intake_link_enforce_one_way_status()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_deal_intake_link_one_way_status ON deal_intake_link")
    op.execute("DROP FUNCTION IF EXISTS deal_intake_link_enforce_one_way_status()")

    op.execute(
        "REVOKE SELECT, UPDATE (status, submitted_at, failed_attempts, last_attempt_at) "
        "ON deal_intake_link FROM dd_public"
    )

    op.execute(
        "REVOKE UPDATE (status, submitted_at, failed_attempts, last_attempt_at) "
        "ON deal_intake_link FROM dd_app"
    )
    op.execute("GRANT UPDATE, DELETE ON deal_intake_link TO dd_app")

    op.execute("ALTER TABLE deal_intake_link NO FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS org_isolation ON deal_intake_link")

    op.execute("DROP INDEX IF EXISTS ux_deal_intake_link_pending_deal")
    op.drop_index(op.f("ix_deal_intake_link_created_by_user_id"), table_name="deal_intake_link")
    op.drop_index(op.f("ix_deal_intake_link_token_hash"), table_name="deal_intake_link")
    op.drop_index(op.f("ix_deal_intake_link_status"), table_name="deal_intake_link")
    op.drop_index(op.f("ix_deal_intake_link_deal_id"), table_name="deal_intake_link")
    op.drop_index(op.f("ix_deal_intake_link_org_id"), table_name="deal_intake_link")
    op.drop_table("deal_intake_link")
