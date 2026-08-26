import hashlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.repo.IntakeLinkRepo import IntakeLinkRepo

_EXPIRES_AT = datetime.now(UTC) + timedelta(days=7)


def _token_hash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _insert_deal(cur, org_pk: int, name: str = "Test Deal") -> str:
    cur.execute("INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id", (org_pk, name))
    return str(cur.fetchone()[0])


@pytest.fixture
def org_a_deal_id(owner_conn, org_a_id) -> str:
    """A deal belonging to org A -- deal_intake_link.deal_id's (real, NOT
    NULL) FK target. No teardown, deliberately -- same reasoning as
    test_data_source_rls.py's org_a_deal_id: db_session's own transaction
    (which may insert rows referencing this deal within a test) is only
    rolled back after fixture teardown runs, so a synchronous DELETE here
    would block on the FK-reference lock, a deadlock, not a real
    correctness issue."""
    with owner_conn.cursor() as cur:
        return _insert_deal(cur, org_a_id, "Org A's deal")


@pytest.fixture
def org_b_intake_link_id(owner_conn) -> Iterator[str]:
    """A deal_intake_link row belonging to a *different* org, seeded via the
    doadmin connection (bypasses RLS) -- a dd_app session scoped to org A's
    app.org_id could never create this row itself.
    """
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
        cur.execute(
            "INSERT INTO deal_intake_link "
            "(org_id, clerk_org_id, deal_id, token_hash, recipient_email, expires_at, "
            "created_by_user_id) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                org_b_pk,
                org_b_clerk_id,
                deal_id,
                _token_hash("org-b-token"),
                "recipient@org-b.example",
                _EXPIRES_AT,
                user_b_pk,
            ),
        )
        link_id = cur.fetchone()[0]

    yield str(link_id)

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM deal_intake_link WHERE id = %s", (link_id,))
        cur.execute("DELETE FROM users WHERE id = %s", (user_b_pk,))
        cur.execute("DELETE FROM deals WHERE id = %s", (deal_id,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_b_pk,))


async def test_org_isolation_hides_other_org_link(db_session, org_a_id, org_b_intake_link_id):
    result = await db_session.execute(
        text("SELECT id FROM deal_intake_link WHERE id = :id"), {"id": org_b_intake_link_id}
    )
    assert result.first() is None

    all_rows = await db_session.execute(text("SELECT id FROM deal_intake_link"))
    assert all(str(row[0]) != org_b_intake_link_id for row in all_rows.fetchall())


async def test_org_isolation_still_shows_own_org_link(
    db_session, org_a_id, org_a_deal_id, user_a_id, org_b_intake_link_id
):
    repo = IntakeLinkRepo(db_session)
    own = await repo.create(
        {
            "org_id": org_a_id,
            "clerk_org_id": "test-tenant-00000000",
            "deal_id": org_a_deal_id,
            "token_hash": _token_hash("org-a-token"),
            "recipient_email": "recipient@org-a.example",
            "expires_at": _EXPIRES_AT,
            "created_by_user_id": user_a_id,
        }
    )
    await db_session.flush()

    fetched = await repo.get_by_id(own.id)
    assert fetched is not None
    assert fetched.recipient_email == "recipient@org-a.example"

    all_rows = await db_session.execute(text("SELECT id FROM deal_intake_link"))
    ids = [str(r[0]) for r in all_rows.fetchall()]
    assert str(own.id) in ids
    assert org_b_intake_link_id not in ids


async def test_clerk_org_id_never_null_on_insert(
    db_session, org_a_id, org_a_deal_id, user_a_id, test_org_id
):
    repo = IntakeLinkRepo(db_session)
    row = await repo.create(
        {
            "org_id": org_a_id,
            "clerk_org_id": test_org_id,
            "deal_id": org_a_deal_id,
            "token_hash": _token_hash("org-a-token-clerk-org-id"),
            "recipient_email": "recipient@org-a.example",
            "expires_at": _EXPIRES_AT,
            "created_by_user_id": user_a_id,
        }
    )
    await db_session.flush()
    await db_session.refresh(row)

    assert row.clerk_org_id == test_org_id


def test_partial_unique_index_blocks_second_pending_link_for_same_deal(
    owner_conn, org_a_id, org_a_deal_id, user_a_id
):
    # owner_conn autocommits and this test has no teardown (same as
    # test_data_source_rls.py's org_a_deal_id precedent) -- token_hash is
    # unique per-run via uuid4 so a second suite run doesn't collide with a
    # row left behind by a previous run.
    run_id = uuid.uuid4().hex[:8]
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deal_intake_link "
            "(org_id, clerk_org_id, deal_id, token_hash, recipient_email, expires_at, "
            "created_by_user_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                org_a_id,
                "test-tenant-00000000",
                org_a_deal_id,
                _token_hash(f"first-pending-{run_id}"),
                "recipient@org-a.example",
                _EXPIRES_AT,
                user_a_id,
            ),
        )

        with pytest.raises(Exception, match="ux_deal_intake_link_pending_deal"):
            cur.execute(
                "INSERT INTO deal_intake_link "
                "(org_id, clerk_org_id, deal_id, token_hash, recipient_email, expires_at, "
                "created_by_user_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    org_a_id,
                    "test-tenant-00000000",
                    org_a_deal_id,
                    _token_hash(f"second-pending-{run_id}"),
                    "recipient@org-a.example",
                    _EXPIRES_AT,
                    user_a_id,
                ),
            )


def test_one_way_status_trigger_blocks_second_update_even_via_table_owner(
    owner_conn, org_a_id, org_a_deal_id, user_a_id
):
    """First UPDATE (pending -> submitted) succeeds. A second UPDATE -- even
    one only touching a granted column (failed_attempts) -- is rejected by
    trg_deal_intake_link_one_way_status, proving the trigger fires against
    the table owner (doadmin) itself, not just dd_app/dd_public."""
    run_id = uuid.uuid4().hex[:8]
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deal_intake_link "
            "(org_id, clerk_org_id, deal_id, token_hash, recipient_email, expires_at, "
            "created_by_user_id) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                org_a_id,
                "test-tenant-00000000",
                org_a_deal_id,
                _token_hash(f"one-way-trigger-{run_id}"),
                "recipient@org-a.example",
                _EXPIRES_AT,
                user_a_id,
            ),
        )
        link_id = cur.fetchone()[0]

        cur.execute(
            "UPDATE deal_intake_link SET status = 'submitted', submitted_at = now() WHERE id = %s",
            (link_id,),
        )

        with pytest.raises(Exception, match="status is final once left pending"):
            cur.execute(
                "UPDATE deal_intake_link SET failed_attempts = failed_attempts + 1 WHERE id = %s",
                (link_id,),
            )


async def test_dd_app_cannot_update_non_granted_column(
    db_session, org_a_id, org_a_deal_id, user_a_id
):
    await db_session.execute(
        text(
            "INSERT INTO deal_intake_link "
            "(org_id, clerk_org_id, deal_id, token_hash, recipient_email, expires_at, "
            "created_by_user_id) VALUES (:org_id, :clerk_org_id, :deal_id, :token_hash, "
            ":recipient_email, :expires_at, :created_by_user_id)"
        ),
        {
            "org_id": org_a_id,
            "clerk_org_id": "test-tenant-00000000",
            "deal_id": org_a_deal_id,
            "token_hash": _token_hash("dd-app-column-grant"),
            "recipient_email": "recipient@org-a.example",
            "expires_at": _EXPIRES_AT,
            "created_by_user_id": user_a_id,
        },
    )
    await db_session.flush()

    # REVOKE UPDATE, DELETE ON deal_intake_link FROM dd_app, narrowed back
    # only for (status, submitted_at, failed_attempts, last_attempt_at) --
    # token_hash is not one of them.
    with pytest.raises(DBAPIError, match="permission denied"):
        await db_session.execute(text("UPDATE deal_intake_link SET token_hash = 'x'"))


async def test_dd_app_cannot_delete_intake_link(db_session, org_a_id, org_a_deal_id, user_a_id):
    await db_session.execute(
        text(
            "INSERT INTO deal_intake_link "
            "(org_id, clerk_org_id, deal_id, token_hash, recipient_email, expires_at, "
            "created_by_user_id) VALUES (:org_id, :clerk_org_id, :deal_id, :token_hash, "
            ":recipient_email, :expires_at, :created_by_user_id)"
        ),
        {
            "org_id": org_a_id,
            "clerk_org_id": "test-tenant-00000000",
            "deal_id": org_a_deal_id,
            "token_hash": _token_hash("dd-app-delete"),
            "recipient_email": "recipient@org-a.example",
            "expires_at": _EXPIRES_AT,
            "created_by_user_id": user_a_id,
        },
    )
    await db_session.flush()

    with pytest.raises(DBAPIError, match="permission denied"):
        await db_session.execute(text("DELETE FROM deal_intake_link"))
