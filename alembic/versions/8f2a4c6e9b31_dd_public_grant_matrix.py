"""dd_public grant matrix

External Deal Intake Link (P1-00): the dd_public grant matrix scoped to the
three tables that already exist today (data_source, organisation,
human_audit_log). dd_public itself is created out-of-band, the same way
dd_app is -- see sandbox/init/02-public-role.sql -- because role creation
with a real password is a one-time, secret-managed operation, not something
Alembic should run against the live cluster. This migration only grants
privileges to a role that migration is assumed to already exist (mirrors how
bootstrap_dd_app_privliges.py never creates dd_app either).

deal_intake_link and deal_intake_response don't exist yet -- their dd_public
grants land in P1-01/P1-02 alongside the migrations that create them, not
here.

- data_source: SELECT + INSERT only (no UPDATE/DELETE) -- the external
  upload flow writes new rows, never edits existing ones. This migration
  grants the bare privilege only; the two matching RLS policies
  (intake_deal_documents, intake_deal_documents_insert) are DEFERRED to
  P1-03's migration (b4f8e1c3a962) -- APPROVED DESIGN CORRECTION (confirmed
  by architect + Vansh, see docs/plans/external-deal-intake-link-status.md's
  Flagged section): those policies need an EXISTS guard against
  deal_intake_link, which doesn't exist until P1-01, so they can't live in
  this migration in ticket order. With RLS enabled+forced on data_source and
  no policy present at this point in the migration chain, the bare grant
  correctly default-denies until P1-03's policies land.
- organisation: three columns of one row (id, name, clerk_org_id). name and
  clerk_org_id are for the intake page's display name; id is not for display
  -- it's needed because the data_source/human_audit_log policies above
  resolve org scope via a subquery on organisation, and Postgres evaluates
  policy USING/WITH CHECK expressions with the querying role's own column
  privileges (no implicit elevation to the policy/table owner's grants) --
  so dd_public needs SELECT on organisation.id for those subqueries to even
  evaluate, not just to satisfy this table's own policy.
- human_audit_log: INSERT only, no SELECT -- every external action gets a
  row, same append-only idiom as dd_app's grant on this table
  (7175bc85ffb0_human_audit_log.py), just narrower (no SELECT for dd_public).

Revision ID: 8f2a4c6e9b31
Revises: 1a2b3c4d5e6f
Create Date: 2026-08-25 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "8f2a4c6e9b31"
down_revision: str | Sequence[str] | None = "1a2b3c4d5e6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("GRANT USAGE ON SCHEMA public TO dd_public")

    # data_source: privilege grant only -- the matching RLS policies
    # (intake_deal_documents, intake_deal_documents_insert) are deferred to
    # P1-03's migration (b4f8e1c3a962), see module docstring. RLS is enabled
    # + forced on data_source elsewhere (its own creation migration), so this
    # bare grant with no policy yet present correctly default-denies dd_public
    # until P1-03's policies land.
    op.execute("GRANT SELECT, INSERT ON data_source TO dd_public")

    # organisation: id (needed for the org-scope subquery above -- see the
    # module docstring) plus name/clerk_org_id for the intake page's display.
    op.execute("GRANT SELECT (id, name, clerk_org_id) ON organisation TO dd_public")
    op.execute("""
        CREATE POLICY intake_organisation_lookup ON organisation
            FOR SELECT TO dd_public
            USING (clerk_org_id = current_setting('app.org_id', true))
    """)

    # human_audit_log: INSERT only, no SELECT. Every external action gets a row.
    op.execute("GRANT INSERT ON human_audit_log TO dd_public")
    op.execute("""
        CREATE POLICY intake_human_audit_insert ON human_audit_log
            FOR INSERT TO dd_public
            WITH CHECK (
                org_id = (SELECT id FROM organisation WHERE clerk_org_id = current_setting('app.org_id', true))
            )
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS intake_human_audit_insert ON human_audit_log")
    op.execute("REVOKE INSERT ON human_audit_log FROM dd_public")

    op.execute("DROP POLICY IF EXISTS intake_organisation_lookup ON organisation")
    op.execute("REVOKE SELECT (id, name, clerk_org_id) ON organisation FROM dd_public")

    op.execute("REVOKE SELECT, INSERT ON data_source FROM dd_public")

    op.execute("REVOKE USAGE ON SCHEMA public FROM dd_public")
