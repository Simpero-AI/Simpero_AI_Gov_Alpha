import hashlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

_EXPIRES_AT = datetime.now(UTC) + timedelta(days=7)
_EXPIRED_AT = datetime.now(UTC) - timedelta(days=1)
_DECLARED_HASH = "a" * 64


def _token_hash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _insert_deal(cur, org_pk: int, name: str = "Test Deal") -> str:
    cur.execute("INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id", (org_pk, name))
    return str(cur.fetchone()[0])


def _insert_data_source(cur, org_pk: int, deal_id: str, storage_key: str) -> str:
    cur.execute(
        "INSERT INTO data_source (org_id, deal_id, storage_key, filename, declared_sha256) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (org_pk, deal_id, storage_key, "intake.pdf", _DECLARED_HASH),
    )
    return str(cur.fetchone()[0])


def _insert_link(
    cur,
    *,
    org_pk: int,
    clerk_org_id: str,
    deal_id: str,
    user_pk: int,
    token_hash: str,
    expires_at: datetime,
    status: str = "pending",
    submitted_at: datetime | None = None,
) -> str:
    cur.execute(
        "INSERT INTO deal_intake_link "
        "(org_id, clerk_org_id, deal_id, token_hash, recipient_email, expires_at, "
        "created_by_user_id, status, submitted_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            org_pk,
            clerk_org_id,
            deal_id,
            token_hash,
            "recipient@org-a.example",
            expires_at,
            user_pk,
            status,
            submitted_at,
        ),
    )
    return str(cur.fetchone()[0])


@pytest.fixture
def org_a_deal_id(owner_conn, org_a_id) -> str:
    """No teardown, deliberately -- same reasoning as
    test_intake_link_rls.py's org_a_deal_id."""
    with owner_conn.cursor() as cur:
        return _insert_deal(cur, org_a_id, "Org A's deal")


@pytest.fixture
def pending_link(owner_conn, org_a_id, org_a_deal_id, user_a_id, test_org_id) -> Iterator[dict]:
    """A single pending, unexpired deal_intake_link row, seeded via
    owner_conn (bypasses RLS). Fresh token per test run via uuid4 so reruns
    don't collide with the partial unique index."""
    run_id = uuid.uuid4().hex[:8]
    token_hash = _token_hash(f"pending-{run_id}")
    with owner_conn.cursor() as cur:
        link_id = _insert_link(
            cur,
            org_pk=org_a_id,
            clerk_org_id=test_org_id,
            deal_id=org_a_deal_id,
            user_pk=user_a_id,
            token_hash=token_hash,
            expires_at=_EXPIRES_AT,
        )

    yield {"id": link_id, "token_hash": token_hash}

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM deal_intake_link WHERE id = %s", (link_id,))


@pytest.fixture
def org_b_docs(owner_conn) -> Iterator[str]:
    """A second org's pending link + data_source row, seeded via owner_conn
    (bypasses RLS) -- proves intake_deal_documents' org_id AND deal_id
    scoping, not just deal_id alone. Modeled on
    tests/test_intake_response_rls.py's org_b_response_id fixture."""
    org_b_clerk_id = f"test-tenant-b-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (org_b_clerk_id, "Org B"),
        )
        org_b_pk = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO users (org_id, role, login_method, clerk_user_id, clerk_org_id, status) "
            "VALUES (%s, 'admin', 'email', %s, %s, 'active') RETURNING id",
            (org_b_pk, f"test-user-b-{uuid.uuid4().hex[:8]}", org_b_clerk_id),
        )
        user_b_pk = cur.fetchone()[0]
        deal_id = _insert_deal(cur, org_b_pk, "Org B's docs deal")
        link_id = _insert_link(
            cur,
            org_pk=org_b_pk,
            clerk_org_id=org_b_clerk_id,
            deal_id=deal_id,
            user_pk=user_b_pk,
            token_hash=_token_hash(f"docs-b-{uuid.uuid4().hex[:8]}"),
            expires_at=_EXPIRES_AT,
        )
        data_source_id = _insert_data_source(cur, org_b_pk, deal_id, "org-b/intake.pdf")

    yield data_source_id

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM data_source WHERE id = %s", (data_source_id,))
        cur.execute("DELETE FROM deal_intake_link WHERE id = %s", (link_id,))
        cur.execute("DELETE FROM users WHERE id = %s", (user_b_pk,))
        cur.execute("DELETE FROM deals WHERE id = %s", (deal_id,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_b_pk,))


@pytest.fixture
def org_a_data_source_id(owner_conn, org_a_id, org_a_deal_id) -> Iterator[str]:
    """A data_source row for org A's own deal (the same deal `pending_link`
    points at) -- for the intra-org scoping test. No dedicated link fixture
    needed here; `pending_link` already provides one for the same deal."""
    with owner_conn.cursor() as cur:
        data_source_id = _insert_data_source(cur, org_a_id, org_a_deal_id, "org-a/intake.pdf")

    yield data_source_id

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM data_source WHERE id = %s", (data_source_id,))


@pytest.fixture
def submitted_link(owner_conn, pending_link) -> dict:
    """Flips `pending_link` to submitted via owner_conn -- the trigger fires
    for doadmin too, so this is a legitimate pending -> submitted
    transition, exercised the same way the real /submit route eventually
    will."""
    with owner_conn.cursor() as cur:
        cur.execute(
            "UPDATE deal_intake_link SET status = 'submitted', submitted_at = now() WHERE id = %s",
            (pending_link["id"],),
        )
    return pending_link


async def _set_guc(session, name: str, value: str) -> None:
    await session.execute(text(f"SELECT set_config('{name}', :v, true)"), {"v": value})


async def test_policy_a_token_hash_reveals_exactly_one_row(public_db_session, pending_link):
    """Only app.intake_token_hash set (app.intake_link_id left unset) --
    exactly one row visible, via policy A specifically."""
    await _set_guc(public_db_session, "app.intake_token_hash", pending_link["token_hash"])
    result = await public_db_session.execute(text("SELECT id FROM deal_intake_link"))
    ids = [str(r[0]) for r in result.fetchall()]
    assert ids == [pending_link["id"]]


async def test_policy_b_alone_does_not_satisfy_policy_a(public_db_session, pending_link):
    """Fresh session (proving policy A's visibility above wasn't an
    accident of policy B): ONLY app.intake_link_id set, first to a
    wrong/different uuid (zero rows), then to the link's real id (one row)
    -- neither GUC satisfies the other's policy."""
    await _set_guc(public_db_session, "app.intake_link_id", str(uuid.uuid4()))
    result = await public_db_session.execute(text("SELECT id FROM deal_intake_link"))
    assert result.fetchall() == []

    await _set_guc(public_db_session, "app.intake_link_id", pending_link["id"])
    result = await public_db_session.execute(text("SELECT id FROM deal_intake_link"))
    ids = [str(r[0]) for r in result.fetchall()]
    assert ids == [pending_link["id"]]


async def test_policy_b_link_id_reveals_exactly_one_row(public_db_session, pending_link):
    """Only app.intake_link_id set (app.intake_token_hash left unset) --
    exactly one row visible, via policy B specifically."""
    await _set_guc(public_db_session, "app.intake_link_id", pending_link["id"])
    result = await public_db_session.execute(text("SELECT id FROM deal_intake_link"))
    ids = [str(r[0]) for r in result.fetchall()]
    assert ids == [pending_link["id"]]


async def test_policy_a_alone_does_not_satisfy_policy_b(public_db_session, pending_link):
    """Fresh session (reverse of the above): ONLY app.intake_token_hash set,
    first to a wrong hash (zero rows), then to the link's real hash (one
    row)."""
    await _set_guc(public_db_session, "app.intake_token_hash", _token_hash("wrong-token"))
    result = await public_db_session.execute(text("SELECT id FROM deal_intake_link"))
    assert result.fetchall() == []

    await _set_guc(public_db_session, "app.intake_token_hash", pending_link["token_hash"])
    result = await public_db_session.execute(text("SELECT id FROM deal_intake_link"))
    ids = [str(r[0]) for r in result.fetchall()]
    assert ids == [pending_link["id"]]


@pytest.mark.parametrize(
    "status,expires_at",
    [
        ("pending", _EXPIRED_AT),  # exercises expires_at > now(), not status
        ("revoked", _EXPIRES_AT),
        # NOT "submitted" here -- APPROVED DESIGN CORRECTION: after the
        # keyhole-policy fix, a submitted link is deliberately visible via
        # the link-id path (intake_session_lookup was widened), so "invisible
        # under both policies" no longer holds for it. That asymmetry
        # (invisible via token-hash, visible via link-id) is covered by
        # test_submitted_link_invisible_via_token_hash_path and
        # test_submitted_link_visible_via_link_id_path below instead.
    ],
)
async def test_non_pending_or_expired_link_invisible_under_both_policies(
    owner_conn,
    org_a_id,
    org_a_deal_id,
    user_a_id,
    test_org_id,
    public_db_session,
    status,
    expires_at,
):
    run_id = uuid.uuid4().hex[:8]
    token_hash = _token_hash(f"terminal-{status}-{run_id}")
    submitted_at = datetime.now(UTC) if status == "submitted" else None
    with owner_conn.cursor() as cur:
        link_id = _insert_link(
            cur,
            org_pk=org_a_id,
            clerk_org_id=test_org_id,
            deal_id=org_a_deal_id,
            user_pk=user_a_id,
            token_hash=token_hash,
            expires_at=expires_at,
            status=status,
            submitted_at=submitted_at,
        )

    try:
        await _set_guc(public_db_session, "app.intake_token_hash", token_hash)
        result = await public_db_session.execute(text("SELECT id FROM deal_intake_link"))
        assert result.fetchall() == []

        await _set_guc(public_db_session, "app.intake_link_id", link_id)
        result = await public_db_session.execute(text("SELECT id FROM deal_intake_link"))
        assert result.fetchall() == []
    finally:
        with owner_conn.cursor() as cur:
            cur.execute("DELETE FROM deal_intake_link WHERE id = %s", (link_id,))


async def test_dd_app_session_keyhole_gucs_have_no_effect(db_session, pending_link):
    """The keyhole policies are TO dd_public only -- a dd_app session with
    either GUC set still relies solely on org_isolation, so it sees nothing
    of another org's link through the keyhole (and this link belongs to
    test_org_id/org A, which db_session is already scoped to via
    org_isolation -- the point is that setting the keyhole GUCs changes
    nothing about what's visible)."""
    before = await db_session.execute(text("SELECT id FROM deal_intake_link"))
    before_ids = {str(r[0]) for r in before.fetchall()}

    await _set_guc(db_session, "app.intake_token_hash", pending_link["token_hash"])
    after_token = await db_session.execute(text("SELECT id FROM deal_intake_link"))
    assert {str(r[0]) for r in after_token.fetchall()} == before_ids

    await _set_guc(db_session, "app.intake_link_id", pending_link["id"])
    after_link_id = await db_session.execute(text("SELECT id FROM deal_intake_link"))
    assert {str(r[0]) for r in after_link_id.fetchall()} == before_ids


async def test_token_hash_path_can_stamp_failed_attempts_but_not_submit(
    public_db_session, pending_link
):
    await _set_guc(public_db_session, "app.intake_token_hash", pending_link["token_hash"])

    # Legitimate /session write: stamping failed_attempts/last_attempt_at
    # while the link stays pending.
    result = await public_db_session.execute(
        text(
            "UPDATE deal_intake_link SET failed_attempts = failed_attempts + 1, "
            "last_attempt_at = now() WHERE token_hash = :h"
        ),
        {"h": pending_link["token_hash"]},
    )
    assert result.rowcount == 1

    # The corrected behavior: the token-hash path must NEVER be able to flip
    # status to submitted -- only the link-id (verified-session) path can.
    with pytest.raises(Exception, match="row-level security"):
        await public_db_session.execute(
            text("UPDATE deal_intake_link SET status = 'submitted' WHERE token_hash = :h"),
            {"h": pending_link["token_hash"]},
        )


async def test_link_id_path_can_flip_status_to_submitted(pending_link, public_db_session):
    # Fixture order matters here (not just style): this UPDATE succeeds and
    # leaves the row locked until public_db_session's fixture teardown calls
    # rollback(). pytest tears sync fixtures down before async ones, in
    # reverse of their setup order -- with (public_db_session, pending_link)
    # as declared, pending_link's synchronous DELETE teardown would run
    # first and block forever on that same lock (the blocking DB call also
    # starves the event loop, so public_db_session's rollback() never gets a
    # chance to run either -- a real deadlock, reproduced against real
    # Postgres). Swapping the order makes public_db_session tear down (and
    # release the lock) first.
    await _set_guc(public_db_session, "app.intake_link_id", pending_link["id"])

    result = await public_db_session.execute(
        text("UPDATE deal_intake_link SET status = 'submitted' WHERE id = :id"),
        {"id": pending_link["id"]},
    )
    assert result.rowcount == 1


async def test_submitted_link_invisible_via_token_hash_path(public_db_session, submitted_link):
    """The design-correction asymmetry, half A: the raw shareable token still
    dies the instant status leaves 'pending' -- intake_token_lookup was
    deliberately NOT widened."""
    await _set_guc(public_db_session, "app.intake_token_hash", submitted_link["token_hash"])
    result = await public_db_session.execute(text("SELECT id FROM deal_intake_link"))
    assert result.fetchall() == []


async def test_submitted_link_visible_via_link_id_path(public_db_session, submitted_link):
    """The design-correction asymmetry, half B: the session/link-id path can
    see its own just-submitted link -- intake_session_lookup was widened to
    admit status = 'submitted', which is what lets the /submit UPDATE's
    resulting row satisfy an applicable SELECT policy."""
    await _set_guc(public_db_session, "app.intake_link_id", submitted_link["id"])
    result = await public_db_session.execute(text("SELECT id FROM deal_intake_link"))
    ids = [str(r[0]) for r in result.fetchall()]
    assert ids == [submitted_link["id"]]


async def test_submitted_link_blocks_data_source_insert(
    public_db_session, test_org_id, org_a_id, org_a_deal_id, submitted_link
):
    """Proves the EXISTS (... status = 'pending') guard on
    intake_deal_documents_insert blocks writes once the link is submitted --
    even though the link row itself is still visible via
    intake_session_lookup (see test_submitted_link_visible_via_link_id_path
    above). Documents become unreachable the moment the link leaves
    'pending', at the DB level."""
    await _set_guc(public_db_session, "app.org_id", test_org_id)
    await _set_guc(public_db_session, "app.intake_deal_id", org_a_deal_id)
    await _set_guc(public_db_session, "app.intake_link_id", submitted_link["id"])

    with pytest.raises(Exception, match="row-level security"):
        await public_db_session.execute(
            text(
                "INSERT INTO data_source (org_id, deal_id, storage_key, filename, declared_sha256) "
                "VALUES (:org_id, :deal_id, :key, :filename, :hash)"
            ),
            {
                "org_id": org_a_id,
                "deal_id": org_a_deal_id,
                "key": "org-a/submitted-attempt.pdf",
                "filename": "submitted-attempt.pdf",
                "hash": "b" * 64,
            },
        )


async def test_submitted_link_blocks_response_insert(
    public_db_session, test_org_id, org_a_id, org_a_deal_id, submitted_link
):
    """Proves the EXISTS (... status = 'pending') guard on
    intake_response_insert (ALTERed here, see module docstring -- the
    original P1-02 policy had no such guard) blocks a second submission once
    the link is submitted, even though the link row itself is still visible
    via intake_session_lookup. MOVED here from
    tests/test_intake_response_rls.py, where it originally lived on P1-02's
    branch before the EXISTS guard's real dependency on this migration's
    keyhole policies was found (see this migration's module docstring)."""
    await _set_guc(public_db_session, "app.org_id", test_org_id)
    await _set_guc(public_db_session, "app.intake_link_id", submitted_link["id"])

    with pytest.raises(Exception, match="row-level security"):
        await public_db_session.execute(
            text(
                "INSERT INTO deal_intake_response (org_id, deal_id, link_id, respondent_email) "
                "VALUES (:org_id, :deal_id, :link_id, :email)"
            ),
            {
                "org_id": org_a_id,
                "deal_id": org_a_deal_id,
                "link_id": submitted_link["id"],
                "email": "respondent@org-a.example",
            },
        )


async def test_response_insert_rejects_deal_id_not_matching_the_links_real_deal(
    public_db_session, owner_conn, test_org_id, org_a_id, pending_link
):
    """Self-review follow-up fix: the WITH CHECK verifies org_id, link_id,
    and status='pending', but originally never verified that
    deal_intake_response.deal_id actually equals the deal_id on the
    deal_intake_link row named by link_id -- deal_id is denormalized purely
    for read convenience (P1-02's migration docstring), so nothing at the DB
    layer stopped a row from being written with a mismatched deal_id. Not
    externally exploitable today (deal_id is always server-derived, never
    client input, per P3-11), but a real gap against this feature's
    provable-at-the-database-layer design pillar (brief section 5.6).
    org_id/link_id/status are all otherwise valid here -- only deal_id is
    wrong (a second, real deal in the same org, so this isn't just an FK
    violation in disguise)."""
    with owner_conn.cursor() as cur:
        wrong_deal_id = _insert_deal(cur, org_a_id, "Org A's OTHER deal")

    await _set_guc(public_db_session, "app.org_id", test_org_id)
    await _set_guc(public_db_session, "app.intake_link_id", pending_link["id"])

    with pytest.raises(Exception, match="row-level security"):
        await public_db_session.execute(
            text(
                "INSERT INTO deal_intake_response (org_id, deal_id, link_id, respondent_email) "
                "VALUES (:org_id, :deal_id, :link_id, :email)"
            ),
            {
                "org_id": org_a_id,
                "deal_id": wrong_deal_id,
                "link_id": pending_link["id"],
                "email": "respondent@org-a.example",
            },
        )


async def test_submitted_link_blocks_data_source_select(
    public_db_session, test_org_id, org_a_id, org_a_deal_id, org_a_data_source_id, submitted_link
):
    """Also-while-here test-coverage fix from the same self-review: the
    EXISTS (... status = 'pending') guard on intake_deal_documents (the
    SELECT policy) was only ever proven via
    test_submitted_link_blocks_data_source_insert -- the INSERT policy's
    identical guard. Proves the SELECT side independently: a submitted
    link's session sees zero data_source rows for its own deal, even though
    org_a_data_source_id is a real row that WOULD be visible while the link
    was still pending (see test_data_source_scoped_to_one_org_deal_link
    below, which proves the pending-link case)."""
    await _set_guc(public_db_session, "app.org_id", test_org_id)
    await _set_guc(public_db_session, "app.intake_deal_id", org_a_deal_id)
    await _set_guc(public_db_session, "app.intake_link_id", submitted_link["id"])

    result = await public_db_session.execute(
        text("SELECT id FROM data_source WHERE id = :id"), {"id": org_a_data_source_id}
    )
    assert result.first() is None


async def test_data_source_scoped_to_one_org_deal_link(
    public_db_session, test_org_id, org_a_deal_id, org_a_data_source_id, org_b_docs, pending_link
):
    """The intra-org data_source scoping test, MOVED here from
    tests/test_dd_public_grant_matrix.py -- see this file's/that file's
    module docstrings. All three GUCs are set together, matching what the
    real P1-04/06 dependency functions will eventually do in one
    coordinated step."""
    await _set_guc(public_db_session, "app.org_id", test_org_id)
    await _set_guc(public_db_session, "app.intake_deal_id", org_a_deal_id)
    await _set_guc(public_db_session, "app.intake_link_id", pending_link["id"])

    result = await public_db_session.execute(text("SELECT id FROM data_source"))
    ids = [str(r[0]) for r in result.fetchall()]

    assert org_a_data_source_id in ids
    assert org_b_docs not in ids
