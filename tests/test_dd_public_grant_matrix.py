"""P1-00 acceptance: dd_public gets exactly the grants the migration adds
(data_source SELECT/INSERT, organisation SELECT(name, clerk_org_id),
human_audit_log INSERT) and nothing else -- everything outside that matrix
is `permission denied`, table by table, not sampled.

The intra-org data_source scoping test that used to live here MOVED to
tests/test_intake_keyhole_policies.py -- APPROVED DESIGN CORRECTION
(confirmed by architect + Vansh, see docs/plans/
external-deal-intake-link-status.md's Flagged section): P1-00's
data_source RLS policies (intake_deal_documents, intake_deal_documents_insert)
were relocated to P1-03's migration (they need an EXISTS guard against
deal_intake_link, which doesn't exist until P1-01), so after this fix P1-00
alone no longer owns any data_source RLS policy -- just the bare grant. The
behavioral scoping test belongs with the policies that actually implement it,
tested at head.

dd_public has no app-level session pool yet (PublicAsyncSessionLocal is
P1-07), so this connects directly with the sandbox/CI credential
sandbox/init/02-public-role.sql creates, same host/port docker-compose.dev.yml
exposes Postgres on.
"""

from collections.abc import Iterator

import psycopg2
import pytest

_DD_PUBLIC_DSN = "postgresql://dd_public:sandbox_dd_public@localhost:5434/simpero"


@pytest.fixture
def dd_public_conn() -> Iterator["psycopg2.extensions.connection"]:
    """Raw dd_public connection -- deliberately not autocommit, so SET LOCAL
    (via set_config(..., true)) scopes to one transaction the same way
    get_db/the future get_public_link_db will use it."""
    conn = psycopg2.connect(_DD_PUBLIC_DSN)
    try:
        yield conn
    finally:
        conn.close()


@pytest.mark.parametrize(
    "table_name",
    ["deals", "mandates", "screening_result", "analysis_run", "users"],
)
def test_dd_public_denied_on_out_of_scope_tables(dd_public_conn, table_name):
    """Not in the P1-00 grant matrix at all -- must be permission denied,
    asserted table by table (not sampled)."""
    with (
        dd_public_conn.cursor() as cur,
        pytest.raises(psycopg2.errors.InsufficientPrivilege, match="permission denied"),
    ):
        cur.execute(f"SELECT 1 FROM {table_name}")  # table_name: fixed parametrize list, not input
    dd_public_conn.rollback()
