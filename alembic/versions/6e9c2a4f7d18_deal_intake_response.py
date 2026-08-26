"""deal intake response

External Deal Intake Link (P1-02): deal_intake_response, the append-only,
tenant-scoped table storing the external party's submitted answers (see
docs/plans/external-deal-intake-link-implementation-brief.md section 2.3).

Blanket-immutable, same idiom as human_audit_log -- NOT deal_intake_link's
narrow lifecycle-column grant-back. An answer submitted by an outside party
is a historical fact, not editable state, so there is no trigger here and no
narrow GRANT UPDATE back to dd_app: REVOKE UPDATE, DELETE ON
deal_intake_response FROM dd_app is the whole story.

dd_public gets INSERT only -- deliberately no SELECT at all (the external
surface never reads answers back, not even its own submission), gated by a
WITH CHECK binding every inserted row to the org named by app.org_id AND the
exact link named by app.intake_link_id -- self-contained (does not depend on
P1-04/P1-06's dependency functions existing yet).

NOTE on the EXISTS(... status = 'pending') guard from the P1-03 design
correction (architect + Vansh, docs/plans/external-deal-intake-link-status.md's
Flagged section): that guard is added by P1-03's migration, not here.
deal_intake_link has RLS+FORCE enabled (P1-01) but dd_public holds no SELECT
policy on it until P1-03 adds the keyhole policies -- an EXISTS subquery
against deal_intake_link baked into THIS migration would see zero rows for
dd_public regardless of the link's real status, rejecting every otherwise-
legitimate INSERT. Confirmed against real Postgres: this is not a hypothetical,
it broke every test in this file that inserts a response. Keep this
migration's WITH CHECK to org_id + link_id only; P1-03 ALTERs the policy once
dd_public can actually see deal_intake_link rows.

Revision ID: 6e9c2a4f7d18
Revises: 3d7b1f5a8c94
Create Date: 2026-08-25 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "6e9c2a4f7d18"
down_revision: str | Sequence[str] | None = "3d7b1f5a8c94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deal_intake_response",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("deal_id", sa.UUID(), nullable=False),
        sa.Column("link_id", sa.UUID(), nullable=False),
        sa.Column("respondent_email", sa.String(length=255), nullable=False),
        sa.Column("answers", postgresql.JSONB(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organisation.id"]),
        sa.ForeignKeyConstraint(["deal_id"], ["deals.id"]),
        sa.ForeignKeyConstraint(["link_id"], ["deal_intake_link.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_deal_intake_response_org_id"), "deal_intake_response", ["org_id"], unique=False
    )
    op.create_index(
        op.f("ix_deal_intake_response_deal_id"), "deal_intake_response", ["deal_id"], unique=False
    )
    op.create_index(
        op.f("ix_deal_intake_response_link_id"), "deal_intake_response", ["link_id"], unique=False
    )

    # Tenant data, so RLS is enabled HERE -- in the same migration that
    # creates the table. Same reasoning as deal_intake_link/data_source: a
    # window with the table unprotected is a window where any org can read
    # another org's submitted answers.
    op.execute("ALTER TABLE deal_intake_response ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY org_isolation ON deal_intake_response
            FOR ALL TO dd_app
            USING (org_id IN (
                SELECT id FROM organisation
                WHERE clerk_org_id = current_setting('app.org_id', true)
            ))
    """)
    # FORCE, on top of ENABLE: a plain ENABLE exempts the table owner
    # (doadmin) from its own RLS policy -- same rationale as deal_intake_link/
    # data_source/human_audit_log.
    op.execute("ALTER TABLE deal_intake_response FORCE ROW LEVEL SECURITY")

    # Blanket immutability -- human_audit_log idiom, no grant-back. SELECT/
    # INSERT for dd_app remain via bootstrap_dd_app_privliges.py's default
    # privileges; only UPDATE/DELETE are revoked.
    op.execute("REVOKE UPDATE, DELETE ON deal_intake_response FROM dd_app")

    # dd_public: INSERT only, deliberately no SELECT grant at all (the
    # external surface never reads answers back, not even its own
    # submission) -- and a full WITH CHECK policy (self-contained, doesn't
    # depend on later tickets).
    op.execute("GRANT INSERT ON deal_intake_response TO dd_public")
    op.execute("""
        CREATE POLICY intake_response_insert ON deal_intake_response
            FOR INSERT TO dd_public
            WITH CHECK (
                org_id = (
                    SELECT id FROM organisation
                    WHERE clerk_org_id = current_setting('app.org_id', true)
                )
                AND link_id = current_setting('app.intake_link_id', true)::uuid
            )
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS intake_response_insert ON deal_intake_response")
    op.execute("REVOKE INSERT ON deal_intake_response FROM dd_public")

    op.execute("GRANT UPDATE, DELETE ON deal_intake_response TO dd_app")

    op.execute("ALTER TABLE deal_intake_response NO FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS org_isolation ON deal_intake_response")

    op.drop_index(op.f("ix_deal_intake_response_link_id"), table_name="deal_intake_response")
    op.drop_index(op.f("ix_deal_intake_response_deal_id"), table_name="deal_intake_response")
    op.drop_index(op.f("ix_deal_intake_response_org_id"), table_name="deal_intake_response")
    op.drop_table("deal_intake_response")
