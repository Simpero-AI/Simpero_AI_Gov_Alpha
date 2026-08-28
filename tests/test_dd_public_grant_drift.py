"""P1-09: the exact-allowlist counterpart to P1-00/05's negative tests.

P1-05's negative tests prove dd_public is denied on *today's* out-of-matrix
tables -- they can't catch a *future* widening, since a newly-granted table
wouldn't be in the sample being checked. This test introspects dd_public's
actual privileges straight from Postgres's own catalogs and asserts the
result equals a hardcoded expected set exactly, both directions: a migration
that adds any grant not in this set fails this test by name; a migration
that removes a grant the flow depends on also fails it.

Uses owner_conn (a catalog query bypassing RLS entirely -- appropriate here,
no app.org_id needed, same idiom as tests/test_dd_public_bypassrls_proof.py's
pg_roles check).

EMPIRICAL NOTE on column_privileges (found while building this test, see
docs/plans/external-deal-intake-link-status.md's P1-09 row): PostgreSQL's
information_schema.column_privileges is NOT limited to column-restricted
grants (e.g. `GRANT SELECT (id, name, clerk_org_id) ON organisation ...`) --
it expands EVERY grant, including whole-table grants with no column list
(e.g. `GRANT SELECT, INSERT ON data_source ...`), into one row per
underlying column. table_privileges, by contrast, only lists whole-table
grants -- a column-restricted grant never appears there at all. Confirmed
directly against real Postgres 16 (docker-compose.dev.yml, port 5434) before
finalizing EXPECTED_COLUMN_PRIVILEGES below, per architect + Vansh's
direction: EXPECTED_COLUMN_PRIVILEGES intentionally includes the full
per-column expansion of dd_public's whole-table grants too, not just the
genuinely column-restricted ones. This is deliberate, not an artifact to
work around -- a whole-table grant genuinely exposes every current column,
and expanding it here means a future migration that adds a column to a
table dd_public already holds a whole-table grant on will correctly fail
this test (the new column silently inherits the grant) instead of that drift
going unnoticed, matching this feature's "loud error in development, never a
quiet leak in production" philosophy (implementation brief, section 4.4/5.4).
"""

EXPECTED_TABLE_PRIVILEGES: set[tuple[str, str]] = {
    ("data_source", "SELECT"),
    ("data_source", "INSERT"),
    ("deal_intake_link", "SELECT"),
    ("deal_intake_response", "INSERT"),
    ("human_audit_log", "INSERT"),
}

EXPECTED_COLUMN_PRIVILEGES: set[tuple[str, str, str]] = {
    # data_source: whole-table `GRANT SELECT, INSERT ON data_source TO
    # dd_public` (P1-00) expands to all 11 of its columns.
    ("data_source", "created_at", "INSERT"),
    ("data_source", "created_at", "SELECT"),
    ("data_source", "deal_id", "INSERT"),
    ("data_source", "deal_id", "SELECT"),
    ("data_source", "declared_sha256", "INSERT"),
    ("data_source", "declared_sha256", "SELECT"),
    ("data_source", "filename", "INSERT"),
    ("data_source", "filename", "SELECT"),
    ("data_source", "fingerprint", "INSERT"),
    ("data_source", "fingerprint", "SELECT"),
    ("data_source", "id", "INSERT"),
    ("data_source", "id", "SELECT"),
    # intake_link_id (P3-10): a new column on an existing whole-table-granted
    # table inherits the same SELECT/INSERT automatically -- no new GRANT
    # statement needed or added.
    ("data_source", "intake_link_id", "INSERT"),
    ("data_source", "intake_link_id", "SELECT"),
    ("data_source", "org_id", "INSERT"),
    ("data_source", "org_id", "SELECT"),
    ("data_source", "status", "INSERT"),
    ("data_source", "status", "SELECT"),
    ("data_source", "status_updated_at", "INSERT"),
    ("data_source", "status_updated_at", "SELECT"),
    ("data_source", "storage_key", "INSERT"),
    ("data_source", "storage_key", "SELECT"),
    # deal_intake_link: whole-table `GRANT SELECT ...` (P1-01) expands to
    # all 15 of its columns, PLUS the genuinely column-restricted
    # `GRANT UPDATE (status, submitted_at, failed_attempts,
    # last_attempt_at) ...` (P1-01) on those 4 lifecycle columns, PLUS the
    # separate `GRANT UPDATE (draft_answers) ...` (P3-09, 2f7e83611f52).
    ("deal_intake_link", "clerk_org_id", "SELECT"),
    ("deal_intake_link", "created_at", "SELECT"),
    ("deal_intake_link", "created_by_user_id", "SELECT"),
    ("deal_intake_link", "deal_id", "SELECT"),
    ("deal_intake_link", "draft_answers", "SELECT"),
    ("deal_intake_link", "draft_answers", "UPDATE"),
    ("deal_intake_link", "expires_at", "SELECT"),
    ("deal_intake_link", "failed_attempts", "SELECT"),
    ("deal_intake_link", "failed_attempts", "UPDATE"),
    ("deal_intake_link", "id", "SELECT"),
    ("deal_intake_link", "last_attempt_at", "SELECT"),
    ("deal_intake_link", "last_attempt_at", "UPDATE"),
    ("deal_intake_link", "org_id", "SELECT"),
    ("deal_intake_link", "questions_snapshot", "SELECT"),
    ("deal_intake_link", "recipient_email", "SELECT"),
    ("deal_intake_link", "status", "SELECT"),
    ("deal_intake_link", "status", "UPDATE"),
    ("deal_intake_link", "submitted_at", "SELECT"),
    ("deal_intake_link", "submitted_at", "UPDATE"),
    ("deal_intake_link", "token_hash", "SELECT"),
    # deal_intake_response: whole-table `GRANT INSERT ...` (P1-02) expands
    # to all 10 of its columns. No SELECT anywhere -- deliberate, the
    # external surface never reads answers back.
    ("deal_intake_response", "answers", "INSERT"),
    ("deal_intake_response", "created_at", "INSERT"),
    ("deal_intake_response", "deal_id", "INSERT"),
    ("deal_intake_response", "id", "INSERT"),
    ("deal_intake_response", "ip_address", "INSERT"),
    ("deal_intake_response", "link_id", "INSERT"),
    ("deal_intake_response", "org_id", "INSERT"),
    ("deal_intake_response", "respondent_email", "INSERT"),
    ("deal_intake_response", "submitted_at", "INSERT"),
    ("deal_intake_response", "user_agent", "INSERT"),
    # human_audit_log: whole-table `GRANT INSERT ...` (P1-00) expands to
    # all 11 of its columns. No SELECT -- append-only, write-only for
    # dd_public.
    ("human_audit_log", "actor_email", "INSERT"),
    ("human_audit_log", "actor_id", "INSERT"),
    ("human_audit_log", "created_at", "INSERT"),
    ("human_audit_log", "deal_id", "INSERT"),
    ("human_audit_log", "event_type", "INSERT"),
    ("human_audit_log", "id", "INSERT"),
    ("human_audit_log", "ip_address", "INSERT"),
    ("human_audit_log", "org_id", "INSERT"),
    ("human_audit_log", "payload", "INSERT"),
    ("human_audit_log", "session_id", "INSERT"),
    ("human_audit_log", "user_agent", "INSERT"),
    # organisation: genuinely column-restricted `GRANT SELECT (id, name,
    # clerk_org_id) ON organisation TO dd_public` (P1-00, corrected -- `id`
    # was added after the RLS-subquery-needs-it discovery, see status.md).
    # No whole-table grant on organisation at all, so no other column shows
    # up here.
    ("organisation", "clerk_org_id", "SELECT"),
    ("organisation", "id", "SELECT"),
    ("organisation", "name", "SELECT"),
}


def test_dd_public_table_privileges_match_exactly(owner_conn) -> None:
    """Whole-table grants only -- column-restricted grants (organisation's
    SELECT, deal_intake_link's UPDATE) deliberately do not appear here at
    all; that's standard information_schema.table_privileges behavior, not
    a gap in this query."""
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, privilege_type FROM information_schema.table_privileges "
            "WHERE grantee = 'dd_public'"
        )
        actual = {(row[0], row[1]) for row in cur.fetchall()}
    assert actual == EXPECTED_TABLE_PRIVILEGES


def test_dd_public_column_privileges_match_exactly(owner_conn) -> None:
    """Every column-level privilege dd_public holds, whether granted via a
    column list or inherited from a whole-table grant (see module
    docstring). Exact set equality: a migration adding a column to a table
    dd_public already holds a whole-table grant on, or adding any new
    grant, fails this test by name."""
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name, privilege_type "
            "FROM information_schema.column_privileges WHERE grantee = 'dd_public'"
        )
        actual = {(row[0], row[1], row[2]) for row in cur.fetchall()}
    assert actual == EXPECTED_COLUMN_PRIVILEGES


def test_dd_public_has_usage_on_public_schema(owner_conn) -> None:
    """Neither information_schema.usage_privileges nor
    information_schema.role_usage_grants carries a row for dd_public's
    `GRANT USAGE ON SCHEMA public` (confirmed empirically) -- so this checks
    the privilege directly via has_schema_privilege, per the ticket's own
    fallback instruction."""
    with owner_conn.cursor() as cur:
        cur.execute("SELECT has_schema_privilege('dd_public', 'public', 'USAGE')")
        (has_usage,) = cur.fetchone()
    assert has_usage is True
