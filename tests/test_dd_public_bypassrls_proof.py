"""P1-08: proof that `dd_public` cannot sidestep RLS via BYPASSRLS or
superuser -- the two role-level attributes that bypass row-level security
regardless of any policy or FORCE setting. See docs/plans/
external-deal-intake-link-implementation-brief.md section 4.6 item 1.
"""

import hashlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

_EXPIRES_AT = datetime.now(UTC) + timedelta(days=7)


def _token_hash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


async def test_dd_public_role_has_neither_bypassrls_nor_superuser(owner_conn):
    """Catalog check: query pg_roles directly via owner_conn (doadmin) --
    this is a catalog read, not something an app session would ever do, so
    bypassing RLS here is appropriate rather than a violation of the
    dd_app/dd_public discipline."""
    with owner_conn.cursor() as cur:
        cur.execute("SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = 'dd_public'")
        row = cur.fetchone()

    assert row is not None
    rolbypassrls, rolsuper = row
    assert rolbypassrls is False
    assert rolsuper is False


@pytest.fixture
def org_a_deal_id(owner_conn, org_a_id) -> str:
    """No teardown, deliberately -- same reasoning as
    tests/test_intake_link_rls.py's org_a_deal_id."""
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (org_a_id, "Org A's deal"),
        )
        return str(cur.fetchone()[0])


@pytest.fixture
def pending_link(owner_conn, org_a_id, org_a_deal_id, user_a_id, test_org_id) -> Iterator[str]:
    """A pending, unexpired deal_intake_link row seeded via owner_conn
    (bypasses RLS) -- same seeding pattern as
    tests/test_intake_link_rls.py / tests/test_intake_keyhole_policies.py."""
    run_id = uuid.uuid4().hex[:8]
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deal_intake_link "
            "(org_id, clerk_org_id, deal_id, token_hash, recipient_email, expires_at, "
            "created_by_user_id) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                org_a_id,
                test_org_id,
                org_a_deal_id,
                _token_hash(f"bypassrls-proof-{run_id}"),
                "recipient@org-a.example",
                _EXPIRES_AT,
                user_a_id,
            ),
        )
        link_id = str(cur.fetchone()[0])

    yield link_id

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM deal_intake_link WHERE id = %s", (link_id,))


async def test_dd_public_no_guc_sees_zero_rows_despite_select_grant(
    public_db_session, pending_link
):
    """Behavioral companion to the catalog check above: a dd_public session
    with NO GUC set at all (no app.intake_token_hash, no app.intake_link_id,
    no app.org_id) still returns zero rows from deal_intake_link, even
    though a row genuinely exists (seeded via owner_conn just above) and
    dd_public holds SELECT on the table (granted in P1-01). This is true
    only if dd_public has neither BYPASSRLS nor superuser -- either one
    would let this query see the row regardless of the (unset) keyhole
    policies. Deliberately zero rows, not permission denied -- dd_public
    DOES have SELECT, so an unfiltered empty result is what proves RLS is
    actually binding, not a grant that happens to be missing."""
    result = await public_db_session.execute(text("SELECT id FROM deal_intake_link"))
    assert result.fetchall() == []
