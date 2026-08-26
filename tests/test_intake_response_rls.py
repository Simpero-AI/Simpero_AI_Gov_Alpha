"""P1-02 acceptance: deal_intake_response is tenant-isolated (org_isolation),
blanket-immutable for dd_app (no narrow exception, unlike deal_intake_link),
and its dd_public INSERT policy's WITH CHECK binds to the exact link named by
app.intake_link_id, not just "some link in this org" -- the cross-tenant
WITH CHECK proof this ticket closes out on P1-00's behalf (see docs/plans/
external-deal-intake-link-status.md).

Modeled on tests/test_intake_link_rls.py's owner_conn seeding patterns and
tests/test_dd_public_grant_matrix.py's dd_public_conn fixture.
"""

import hashlib
import os
import uuid
from collections.abc import Iterator

import psycopg2
import psycopg2.errors
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

_DD_PUBLIC_DSN = os.environ["PUBLIC_DATABASE_URL"].replace("+psycopg2", "").replace("+asyncpg", "")


def _token_hash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _insert_deal(cur, org_pk: int, name: str = "Test Deal") -> str:
    cur.execute("INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id", (org_pk, name))
    return str(cur.fetchone()[0])


def _insert_pending_link(
    cur, org_pk: int, clerk_org_id: str, deal_id: str, token_seed: str, user_pk: int
) -> str:
    cur.execute(
        "INSERT INTO deal_intake_link "
        "(org_id, clerk_org_id, deal_id, token_hash, recipient_email, expires_at, "
        "created_by_user_id) VALUES (%s, %s, %s, %s, %s, now() + interval '7 days', %s) "
        "RETURNING id",
        (org_pk, clerk_org_id, deal_id, _token_hash(token_seed), "recipient@example.com", user_pk),
    )
    return str(cur.fetchone()[0])


@pytest.fixture
def dd_public_conn() -> Iterator["psycopg2.extensions.connection"]:
    """Raw dd_public connection -- same pattern as
    tests/test_dd_public_grant_matrix.py's fixture of the same name.
    dd_public has no app-level session pool yet (PublicAsyncSessionLocal is
    P1-07)."""
    conn = psycopg2.connect(_DD_PUBLIC_DSN)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def org_a_deal_id(owner_conn, org_a_id) -> str:
    """No teardown, deliberately -- same reasoning as
    test_intake_link_rls.py's fixture of the same name."""
    with owner_conn.cursor() as cur:
        return _insert_deal(cur, org_a_id, "Org A's deal")


@pytest.fixture
def org_a_link_id(owner_conn, org_a_id, org_a_deal_id, user_a_id) -> str:
    """A pending deal_intake_link in org A, for deal_intake_response.link_id's
    (real, NOT NULL) FK target. No teardown, deliberately -- token_hash is
    unique per-run via uuid4 so a second suite run doesn't collide with a row
    left behind by a previous run (same reasoning as
    test_intake_link_rls.py's partial-unique-index test)."""
    with owner_conn.cursor() as cur:
        return _insert_pending_link(
            cur,
            org_a_id,
            "test-tenant-00000000",
            org_a_deal_id,
            f"response-rls-org-a-{uuid.uuid4().hex[:8]}",
            user_a_id,
        )


@pytest.fixture
def org_a_response_id(owner_conn, org_a_id, org_a_deal_id, org_a_link_id) -> Iterator[str]:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deal_intake_response (org_id, deal_id, link_id, respondent_email) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (org_a_id, org_a_deal_id, org_a_link_id, "respondent@org-a.example"),
        )
        response_id = cur.fetchone()[0]

    yield str(response_id)

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM deal_intake_response WHERE id = %s", (response_id,))


@pytest.fixture
def org_b_response_id(owner_conn) -> Iterator[str]:
    """A deal_intake_response row belonging to a *different* org, seeded via
    the doadmin connection (bypasses RLS) -- a dd_app session scoped to org
    A's app.org_id could never create this row itself."""
    org_b_clerk_id = f"test-tenant-b-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (org_b_clerk_id, "Org B"),
        )
        org_b_pk = cur.fetchone()[0]
        deal_id = _insert_deal(cur, org_b_pk, "Org B's deal")
        cur.execute(
            "INSERT INTO users (org_id, role, login_method, clerk_user_id, clerk_org_id, status) "
            "VALUES (%s, 'admin', 'email', %s, %s, 'active') RETURNING id",
            (org_b_pk, f"test-user-b-{uuid.uuid4().hex[:8]}", org_b_clerk_id),
        )
        user_b_pk = cur.fetchone()[0]
        link_id = _insert_pending_link(
            cur,
            org_b_pk,
            org_b_clerk_id,
            deal_id,
            f"response-rls-org-b-{uuid.uuid4().hex[:8]}",
            user_b_pk,
        )
        cur.execute(
            "INSERT INTO deal_intake_response (org_id, deal_id, link_id, respondent_email) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (org_b_pk, deal_id, link_id, "respondent@org-b.example"),
        )
        response_id = cur.fetchone()[0]

    yield str(response_id)

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM deal_intake_response WHERE id = %s", (response_id,))
        cur.execute("DELETE FROM deal_intake_link WHERE id = %s", (link_id,))
        cur.execute("DELETE FROM users WHERE id = %s", (user_b_pk,))
        cur.execute("DELETE FROM deals WHERE id = %s", (deal_id,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_b_pk,))


@pytest.fixture
def org_a_link_used(owner_conn, org_a_id, user_a_id) -> tuple[str, str]:
    """The link a dd_public session's app.intake_link_id will name -- its own
    deal, so a matching INSERT satisfies the WITH CHECK. No teardown."""
    with owner_conn.cursor() as cur:
        deal_id = _insert_deal(cur, org_a_id, "Org A's used-link deal")
        link_id = _insert_pending_link(
            cur,
            org_a_id,
            "test-tenant-00000000",
            deal_id,
            f"response-with-check-used-{uuid.uuid4().hex[:8]}",
            user_a_id,
        )
    return deal_id, link_id


@pytest.fixture
def org_a_link_other(owner_conn, org_a_id, user_a_id) -> tuple[str, str]:
    """A second, different link -- also in org A -- proving the WITH CHECK
    binds to the exact link named by the GUC, not just "some link in this
    org". No teardown."""
    with owner_conn.cursor() as cur:
        deal_id = _insert_deal(cur, org_a_id, "Org A's other-link deal")
        link_id = _insert_pending_link(
            cur,
            org_a_id,
            "test-tenant-00000000",
            deal_id,
            f"response-with-check-other-{uuid.uuid4().hex[:8]}",
            user_a_id,
        )
    return deal_id, link_id


async def test_org_isolation_hides_other_org_response(db_session, org_a_id, org_b_response_id):
    result = await db_session.execute(
        text("SELECT id FROM deal_intake_response WHERE id = :id"), {"id": org_b_response_id}
    )
    assert result.first() is None


async def test_org_isolation_shows_own_org_response(
    db_session, org_a_id, org_a_response_id, org_b_response_id
):
    result = await db_session.execute(
        text("SELECT id FROM deal_intake_response WHERE id = :id"), {"id": org_a_response_id}
    )
    assert result.first() is not None

    all_rows = await db_session.execute(text("SELECT id FROM deal_intake_response"))
    ids = [str(row[0]) for row in all_rows.fetchall()]
    assert org_a_response_id in ids
    assert org_b_response_id not in ids


async def test_dd_app_cannot_update_deal_intake_response(
    db_session, org_a_id, org_a_deal_id, org_a_link_id
):
    await db_session.execute(
        text(
            "INSERT INTO deal_intake_response (org_id, deal_id, link_id, respondent_email) "
            "VALUES (:org_id, :deal_id, :link_id, :email)"
        ),
        {
            "org_id": org_a_id,
            "deal_id": org_a_deal_id,
            "link_id": org_a_link_id,
            "email": "respondent@org-a.example",
        },
    )
    await db_session.flush()

    # Blanket REVOKE UPDATE, DELETE ON deal_intake_response FROM dd_app --
    # unlike deal_intake_link, there is no narrow exception here. Must fail
    # at the database, not be caught by any application-level guard (same
    # assertion style as tests/test_human_audit_log_immutability.py).
    with pytest.raises(DBAPIError, match="permission denied"):
        await db_session.execute(
            text("UPDATE deal_intake_response SET respondent_email = 'tampered@example.com'")
        )


async def test_dd_app_cannot_delete_deal_intake_response(
    db_session, org_a_id, org_a_deal_id, org_a_link_id
):
    await db_session.execute(
        text(
            "INSERT INTO deal_intake_response (org_id, deal_id, link_id, respondent_email) "
            "VALUES (:org_id, :deal_id, :link_id, :email)"
        ),
        {
            "org_id": org_a_id,
            "deal_id": org_a_deal_id,
            "link_id": org_a_link_id,
            "email": "respondent@org-a.example",
        },
    )
    await db_session.flush()

    with pytest.raises(DBAPIError, match="permission denied"):
        await db_session.execute(text("DELETE FROM deal_intake_response"))


def test_dd_public_cannot_insert_response_for_submitted_link(
    dd_public_conn, owner_conn, org_a_id, test_org_id, org_a_link_used
):
    """APPROVED DESIGN CORRECTION (confirmed by architect + Vansh, see
    docs/plans/external-deal-intake-link-status.md's Flagged section):
    intake_response_insert's WITH CHECK now also requires
    EXISTS (... status = 'pending') -- flip the link to submitted via
    owner_conn, then confirm the INSERT is rejected."""
    used_deal_id, used_link_id = org_a_link_used
    with owner_conn.cursor() as cur:
        cur.execute(
            "UPDATE deal_intake_link SET status = 'submitted', submitted_at = now() WHERE id = %s",
            (used_link_id,),
        )

    with dd_public_conn.cursor() as cur:
        cur.execute("SELECT set_config('app.org_id', %s, true)", (test_org_id,))
        cur.execute("SELECT set_config('app.intake_link_id', %s, true)", (used_link_id,))

        with pytest.raises(
            psycopg2.errors.InsufficientPrivilege, match="row-level security policy"
        ):
            cur.execute(
                "INSERT INTO deal_intake_response (org_id, deal_id, link_id, respondent_email) "
                "VALUES (%s, %s, %s, %s)",
                (org_a_id, used_deal_id, used_link_id, "respondent@org-a.example"),
            )

    dd_public_conn.rollback()


def test_dd_public_with_check_binds_to_exact_link(
    dd_public_conn, org_a_id, test_org_id, org_a_link_used, org_a_link_other
):
    used_deal_id, used_link_id = org_a_link_used
    _other_deal_id, other_link_id = org_a_link_other

    with dd_public_conn.cursor() as cur:
        cur.execute("SELECT set_config('app.org_id', %s, true)", (test_org_id,))
        cur.execute("SELECT set_config('app.intake_link_id', %s, true)", (used_link_id,))

        # The session's own link -- WITH CHECK is satisfied, INSERT succeeds.
        cur.execute(
            "INSERT INTO deal_intake_response (org_id, deal_id, link_id, respondent_email) "
            "VALUES (%s, %s, %s, %s)",
            (org_a_id, used_deal_id, used_link_id, "respondent@org-a.example"),
        )

        # A *different* link, still nominally in org A -- the WITH CHECK
        # binds to the exact link the GUC names, not just "some link in this
        # org", so this is rejected.
        with pytest.raises(
            psycopg2.errors.InsufficientPrivilege, match="row-level security policy"
        ):
            cur.execute(
                "INSERT INTO deal_intake_response (org_id, deal_id, link_id, respondent_email) "
                "VALUES (%s, %s, %s, %s)",
                (org_a_id, used_deal_id, other_link_id, "respondent@org-a.example"),
            )

    dd_public_conn.rollback()
