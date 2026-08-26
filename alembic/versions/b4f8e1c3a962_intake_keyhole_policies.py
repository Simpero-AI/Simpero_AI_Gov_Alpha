"""intake keyhole policies

External Deal Intake Link (P1-03): the two dd_public "keyhole" SELECT
policies on deal_intake_link, the dd_public UPDATE policy, and (moved here
from P1-00, see below) the two dd_public policies on data_source (see
docs/plans/external-deal-intake-link-implementation-brief.md section 4.2).

Both SELECT policies are keyholes, not doors -- each reveals exactly one row,
and only to a caller who already holds the matching secret (a token hash, or
a link id that only exists in app.intake_link_id after a session JWT has
been verified). Both are TO dd_public only -- dd_app reaches deal_intake_link
through org_isolation and never needs a keyhole.

APPROVED DESIGN CORRECTION (confirmed by architect + Vansh, see
docs/plans/external-deal-intake-link-status.md's Flagged section): the
original version of this migration required BOTH keyhole SELECT policies to
enforce status = 'pending'. Postgres requires the row *resulting* from an
UPDATE to satisfy an applicable SELECT policy for the querying role, so that
made it structurally impossible for the link-id/session path to ever
complete `UPDATE ... SET status = 'submitted'` -- reproduced against real
Postgres 16, including an isolated minimal-schema repro. The fix: widen
intake_session_lookup (only) to admit status IN ('pending', 'submitted').
This doesn't reopen enumeration -- the link-id path is reached only after a
verified session JWT names this exact link_id, never by guessing a UUID.
intake_token_lookup is deliberately NOT widened -- the raw shareable token
still dies the instant status leaves 'pending'.

The UPDATE policy's WITH CHECK is a deliberate correction to the brief's own
section 4.2, which only sketches this policy in prose: the token-hash path
(POST /session, P3) may stamp failed_attempts/last_attempt_at while the link
stays pending, but must never be able to flip status to 'submitted' --  only
the link-id path (POST /submit, P3, reachable only after a session JWT is
verified) can do that. This makes "submission requires a verified session" a
database-enforced fact, not a route-code convention.

data_source's two policies (intake_deal_documents, intake_deal_documents_insert)
are MOVED HERE from P1-00's migration -- P1-00 predates deal_intake_link in
ticket order, and these policies need the EXISTS guard below, which didn't
exist until this fix. Both gain an EXISTS guard tying document access to a
still-pending link for that deal -- this is what makes "documents become
unreachable once the link is submitted/revoked/expired" true at the DB
level, not just an app-code convention. The EXISTS subquery is itself
subject to deal_intake_link's own RLS for dd_public (intake_token_lookup /
intake_session_lookup above) -- it only resolves to a visible row when the
caller's own GUCs (app.intake_token_hash or app.intake_link_id) already
correctly identify that exact link, so no new privilege surface is opened
here. The underlying GRANT SELECT, INSERT ON data_source TO dd_public stays
in P1-00's migration (privilege-only) -- not re-granted here.

deal_intake_response's intake_response_insert policy (P1-02) is ALTERed here
to add the same EXISTS(... status = 'pending') guard, for the same reason
and by the same architect + Vansh correction -- and for the same structural
reason it can't live on P1-02's own migration: the EXISTS subquery needs
dd_public to have SOME visible row in deal_intake_link, which only becomes
true once intake_token_lookup / intake_session_lookup exist (above, this
same migration). Confirmed against real Postgres: baking this guard into
P1-02 directly made every response INSERT fail outright, not just
RLS-filter correctly, because dd_public could see zero deal_intake_link rows
at that point in the migration chain.

Revision ID: b4f8e1c3a962
Revises: 6e9c2a4f7d18
Create Date: 2026-08-25 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b4f8e1c3a962"
down_revision: str | Sequence[str] | None = "6e9c2a4f7d18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Policy A: for the future /session endpoint (P3) only. Keyed on the
    # long-lived link token the recipient actually holds (its SHA-256 hash).
    # Only reveals a link that is still pending and unexpired -- an expired,
    # revoked, or already-submitted link is invisible even to someone holding
    # the correct token. Deliberately NOT widened -- see module docstring.
    op.execute("""
        CREATE POLICY intake_token_lookup ON deal_intake_link
            FOR SELECT TO dd_public
            USING (
                token_hash = current_setting('app.intake_token_hash', true)
                AND status = 'pending'
                AND expires_at > now()
            )
    """)

    # Policy B: for every future route AFTER /session (P3). Keyed on the
    # link's own id, which only ends up in app.intake_link_id after the
    # session JWT has been verified. The raw token never appears again past
    # this point. WIDENED to admit 'submitted' -- see module docstring for
    # why this is what makes the /submit UPDATE completable at all.
    op.execute("""
        CREATE POLICY intake_session_lookup ON deal_intake_link
            FOR SELECT TO dd_public
            USING (
                id = current_setting('app.intake_link_id', true)::uuid
                AND status IN ('pending', 'submitted')
                AND expires_at > now()
            )
    """)

    # UPDATE policy: the only two legitimate dd_public writes are (1)
    # /session stamping failed_attempts/last_attempt_at while the link stays
    # pending, and (2) /submit flipping status to submitted. The WITH CHECK
    # deliberately does NOT allow the token-hash path to reach 'submitted' --
    # see module docstring.
    op.execute("""
        CREATE POLICY intake_link_status_update ON deal_intake_link
            FOR UPDATE TO dd_public
            USING (
                (token_hash = current_setting('app.intake_token_hash', true) OR id = current_setting('app.intake_link_id', true)::uuid)
                AND status = 'pending'
                AND expires_at > now()
            )
            WITH CHECK (
                (token_hash = current_setting('app.intake_token_hash', true) AND status = 'pending')
                OR
                (id = current_setting('app.intake_link_id', true)::uuid AND status IN ('pending', 'submitted'))
            )
    """)

    # data_source: MOVED HERE from P1-00 -- see module docstring. The GRANT
    # SELECT, INSERT ON data_source TO dd_public stays in P1-00's migration;
    # not re-granted here.
    op.execute("""
        CREATE POLICY intake_deal_documents ON data_source
            FOR SELECT TO dd_public
            USING (
                org_id = (SELECT id FROM organisation WHERE clerk_org_id = current_setting('app.org_id', true))
                AND deal_id = current_setting('app.intake_deal_id', true)::uuid
                AND EXISTS (
                    SELECT 1 FROM deal_intake_link l
                    WHERE l.deal_id = data_source.deal_id AND l.status = 'pending'
                )
            )
    """)
    op.execute("""
        CREATE POLICY intake_deal_documents_insert ON data_source
            FOR INSERT TO dd_public
            WITH CHECK (
                org_id = (SELECT id FROM organisation WHERE clerk_org_id = current_setting('app.org_id', true))
                AND deal_id = current_setting('app.intake_deal_id', true)::uuid
                AND EXISTS (
                    SELECT 1 FROM deal_intake_link l
                    WHERE l.deal_id = data_source.deal_id AND l.status = 'pending'
                )
            )
    """)

    # deal_intake_response: ALTER the P1-02 policy to add the EXISTS guard --
    # see module docstring for why this can't live on P1-02's own migration.
    op.execute("""
        ALTER POLICY intake_response_insert ON deal_intake_response
            WITH CHECK (
                org_id = (
                    SELECT id FROM organisation
                    WHERE clerk_org_id = current_setting('app.org_id', true)
                )
                AND link_id = current_setting('app.intake_link_id', true)::uuid
                AND EXISTS (
                    SELECT 1 FROM deal_intake_link l
                    WHERE l.id = deal_intake_response.link_id AND l.status = 'pending'
                )
            )
    """)


def downgrade() -> None:
    # Revert intake_response_insert to P1-02's original WITH CHECK (no
    # EXISTS guard) before dropping the policies it depends on.
    op.execute("""
        ALTER POLICY intake_response_insert ON deal_intake_response
            WITH CHECK (
                org_id = (
                    SELECT id FROM organisation
                    WHERE clerk_org_id = current_setting('app.org_id', true)
                )
                AND link_id = current_setting('app.intake_link_id', true)::uuid
            )
    """)

    op.execute("DROP POLICY IF EXISTS intake_deal_documents_insert ON data_source")
    op.execute("DROP POLICY IF EXISTS intake_deal_documents ON data_source")

    op.execute("DROP POLICY IF EXISTS intake_link_status_update ON deal_intake_link")
    op.execute("DROP POLICY IF EXISTS intake_session_lookup ON deal_intake_link")
    op.execute("DROP POLICY IF EXISTS intake_token_lookup ON deal_intake_link")
