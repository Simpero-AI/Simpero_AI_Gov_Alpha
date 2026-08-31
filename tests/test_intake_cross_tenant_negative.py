"""P1-05: cross-tenant RLS + role-privilege negative test suite. This is the
last P1 ticket -- "P1 is done when P1-05 is green" (implementation brief,
section 6).

This is a SYNTHESIS test file, not new mechanism: every individual assertion
below is already covered by an earlier ticket's test in isolation (P1-04,
P1-06, P1-08, P1-09, P1-03's own keyhole-policy tests). This file exists to
prove they all hold TOGETHER, end-to-end, through the actual public
dependency functions (get_public_link_db / get_public_session_db) -- not
just against raw policy predicates via a bare public_db_session -- closing
out the full cross-tenant + role-privilege story in one gating suite. A few
lines duplicated from earlier tickets' tests is deliberate here, not
sloppiness -- see the individual test docstrings below.

IMPORTANT -- tested against the CORRECTED (post-P1-03-fix) behavior, not the
implementation brief's original stale wording. Per docs/plans/
external-deal-intake-link-status.md's P1-03 row and its two "Flagged"
entries: intake_session_lookup (the link-id/session path) was deliberately
widened to also admit status = 'submitted', so a submitted link is invisible
via the raw-token path (get_public_link_db) but VISIBLE via the session path
(get_public_session_db) -- an intentional asymmetry, approved by architect
and Vansh, not a bug. Expired/revoked links remain invisible via BOTH paths,
unchanged from the brief's original wording.
"""

import hashlib
import os
import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg2
import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.core.intake_security import encode_intake_session_jwt, sha256_hex
from app.core.public_dependencies import get_public_link_db, get_public_session_db

_EXPIRES_AT = datetime.now(UTC) + timedelta(days=7)
_EXPIRED_AT = datetime.now(UTC) - timedelta(days=1)
_DECLARED_HASH = "a" * 64
_DD_PUBLIC_DSN = os.environ["PUBLIC_DATABASE_URL"].replace("+psycopg2", "").replace("+asyncpg", "")


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
) -> str:
    cur.execute(
        "INSERT INTO deal_intake_link "
        "(org_id, clerk_org_id, deal_id, token_hash, recipient_email, expires_at, "
        "created_by_user_id, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            org_pk,
            clerk_org_id,
            deal_id,
            token_hash,
            "recipient@org-a.example",
            expires_at,
            user_pk,
            status,
        ),
    )
    return str(cur.fetchone()[0])


@pytest.fixture
def org_a_deal_id(owner_conn, org_a_id) -> str:
    """No teardown, deliberately -- same reasoning as
    tests/test_intake_link_rls.py's org_a_deal_id."""
    with owner_conn.cursor() as cur:
        return _insert_deal(cur, org_a_id, "Org A's deal")


@pytest.fixture
def org_a_link(owner_conn, org_a_id, org_a_deal_id, user_a_id, test_org_id) -> Iterator[dict]:
    """Org A's own pending link (raw token controlled by the test, never
    stored -- only its SHA-256) plus a data_source row for the same deal --
    the seed for items 1/2/3's cross-tenant proofs through the real
    dependency functions."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = sha256_hex(raw_token)
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
        data_source_id = _insert_data_source(cur, org_a_id, org_a_deal_id, "org-a/intake.pdf")

    yield {
        "id": link_id,
        "raw_token": raw_token,
        "data_source_id": data_source_id,
    }

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM data_source WHERE id = %s", (data_source_id,))
        cur.execute("DELETE FROM deal_intake_link WHERE id = %s", (link_id,))


@pytest.fixture
def org_b(owner_conn) -> Iterator[dict]:
    """A second, fully independent org -- its own organisation/user/deal,
    plus its own pending link + data_source row -- so items 1/2 prove zero
    org-B rows against a real second org actually present, not just an
    empty table. Same shape as test_intake_keyhole_policies.py's/
    test_public_dependencies.py's org_b_docs fixture."""
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
        deal_id = _insert_deal(cur, org_b_pk, "Org B's deal")
        link_id = _insert_link(
            cur,
            org_pk=org_b_pk,
            clerk_org_id=org_b_clerk_id,
            deal_id=deal_id,
            user_pk=user_b_pk,
            token_hash=_token_hash(f"org-b-{uuid.uuid4().hex[:8]}"),
            expires_at=_EXPIRES_AT,
        )
        data_source_id = _insert_data_source(cur, org_b_pk, deal_id, "org-b/intake.pdf")

    yield {"data_source_id": data_source_id}

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM data_source WHERE id = %s", (data_source_id,))
        cur.execute("DELETE FROM deal_intake_link WHERE id = %s", (link_id,))
        cur.execute("DELETE FROM users WHERE id = %s", (user_b_pk,))
        cur.execute("DELETE FROM deals WHERE id = %s", (deal_id,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_b_pk,))


@pytest.fixture
def expired_link(owner_conn, org_a_id, org_a_deal_id, user_a_id, test_org_id) -> Iterator[dict]:
    """A `pending` link whose expires_at is already in the past -- the lazy-
    expiry case (brief section 2.2): still stored as `pending`, but both
    keyhole policies' `expires_at > now()` clause makes it invisible."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = sha256_hex(raw_token)
    with owner_conn.cursor() as cur:
        link_id = _insert_link(
            cur,
            org_pk=org_a_id,
            clerk_org_id=test_org_id,
            deal_id=org_a_deal_id,
            user_pk=user_a_id,
            token_hash=token_hash,
            expires_at=_EXPIRED_AT,
        )

    yield {"id": link_id, "raw_token": raw_token}

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM deal_intake_link WHERE id = %s", (link_id,))


@pytest.fixture
def revoked_link(owner_conn, org_a_id, org_a_deal_id, user_a_id, test_org_id) -> Iterator[dict]:
    """A link inserted directly as `revoked` (the one-way trigger only guards
    UPDATE, not INSERT, so this is a legitimate way to seed a terminal-state
    row without exercising the trigger)."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = sha256_hex(raw_token)
    with owner_conn.cursor() as cur:
        link_id = _insert_link(
            cur,
            org_pk=org_a_id,
            clerk_org_id=test_org_id,
            deal_id=org_a_deal_id,
            user_pk=user_a_id,
            token_hash=token_hash,
            expires_at=_EXPIRES_AT,
            status="revoked",
        )

    yield {"id": link_id, "raw_token": raw_token}

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM deal_intake_link WHERE id = %s", (link_id,))


@pytest.fixture
def submitted_link(owner_conn, org_a_id, org_a_deal_id, user_a_id, test_org_id) -> Iterator[dict]:
    """A link flipped pending -> submitted via owner_conn -- the trigger
    fires for doadmin too, so this is a legitimate transition, exercised the
    same way the real /submit route eventually will (P1-03's own
    `submitted_link` fixture, reproduced here since this file seeds its own
    raw token rather than reusing test_intake_keyhole_policies.py's
    `pending_link`)."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = sha256_hex(raw_token)
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
        cur.execute(
            "UPDATE deal_intake_link SET status = 'submitted', submitted_at = now() WHERE id = %s",
            (link_id,),
        )

    yield {"id": link_id, "raw_token": raw_token}

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM deal_intake_link WHERE id = %s", (link_id,))


@pytest.fixture
def dd_public_conn() -> Iterator["psycopg2.extensions.connection"]:
    """Raw dd_public connection -- same DSN/pattern as
    tests/test_dd_public_grant_matrix.py's own fixture, reused here for
    item 7's closing-gate re-run."""
    conn = psycopg2.connect(_DD_PUBLIC_DSN)
    try:
        yield conn
    finally:
        conn.close()


# --- 1/2: cross-tenant via each public dependency function -----------------


async def test_cross_tenant_via_get_public_link_db(org_a_link, org_b):
    """Re-confirms P1-04's own test end-to-end with a second org actually
    present, which P1-04's test alone didn't need."""
    agen = get_public_link_db(org_a_link["raw_token"])
    session, _link = await agen.__anext__()
    try:
        result = await session.execute(text("SELECT id FROM data_source"))
        ids = [str(r[0]) for r in result.fetchall()]
        assert org_a_link["data_source_id"] in ids
        assert org_b["data_source_id"] not in ids
    finally:
        await agen.aclose()


async def test_cross_tenant_via_get_public_session_db(org_a_link, org_b):
    """Same as above, via the session-JWT path."""
    session_token = encode_intake_session_jwt(uuid.UUID(org_a_link["id"]), "respondent@example.com")
    agen = get_public_session_db(f"Bearer {session_token}")
    session, _link = await agen.__anext__()
    try:
        result = await session.execute(text("SELECT id FROM data_source"))
        ids = [str(r[0]) for r in result.fetchall()]
        assert org_a_link["data_source_id"] in ids
        assert org_b["data_source_id"] not in ids
    finally:
        await agen.aclose()


# --- 3: no GUC set -> zero rows under either keyhole policy -----------------


async def test_no_guc_set_sees_zero_rows_under_either_keyhole_policy(public_db_session, org_a_link):
    """Re-proves P1-08's test_dd_public_no_guc_sees_zero_rows_despite_select_grant
    as part of the closing gate -- a bare public_db_session with nothing set,
    against a table dd_public genuinely holds SELECT on, with a real row
    present (org_a_link, seeded via owner_conn just above). Deliberately
    duplicated rather than imported: this file's whole purpose is redundant
    end-to-end proof, per the ticket's own description."""
    result = await public_db_session.execute(text("SELECT id FROM deal_intake_link"))
    assert result.fetchall() == []


# --- 4: expired/revoked -> 404 from BOTH dependency functions --------------


async def test_expired_link_404s_via_get_public_link_db(expired_link):
    agen = get_public_link_db(expired_link["raw_token"])
    with pytest.raises(HTTPException) as exc_info:
        await agen.__anext__()
    assert exc_info.value.status_code == 404
    await agen.aclose()


async def test_expired_link_404s_via_get_public_session_db(expired_link):
    session_token = encode_intake_session_jwt(
        uuid.UUID(expired_link["id"]), "respondent@example.com"
    )
    agen = get_public_session_db(f"Bearer {session_token}")
    with pytest.raises(HTTPException) as exc_info:
        await agen.__anext__()
    assert exc_info.value.status_code == 404
    await agen.aclose()


async def test_revoked_link_404s_via_get_public_link_db(revoked_link):
    agen = get_public_link_db(revoked_link["raw_token"])
    with pytest.raises(HTTPException) as exc_info:
        await agen.__anext__()
    assert exc_info.value.status_code == 404
    await agen.aclose()


async def test_revoked_link_404s_via_get_public_session_db(revoked_link):
    session_token = encode_intake_session_jwt(
        uuid.UUID(revoked_link["id"]), "respondent@example.com"
    )
    agen = get_public_session_db(f"Bearer {session_token}")
    with pytest.raises(HTTPException) as exc_info:
        await agen.__anext__()
    assert exc_info.value.status_code == 404
    await agen.aclose()


# --- 5: submitted -> the corrected asymmetric behavior, through the actual -
#        dependency functions (not just raw policy predicates) -------------


async def test_submitted_link_asymmetry_through_dependency_functions(submitted_link):
    """The same property P1-03 already proved at the raw-policy level
    (test_submitted_link_invisible_via_token_hash_path /
    test_submitted_link_visible_via_link_id_path in
    tests/test_intake_keyhole_policies.py) -- proven here through the actual
    dependency functions used by (future) real routes. Token path: still
    blind to a submitted link, 404. Session path: can see its own
    just-submitted state -- an open session tab can observe its own link
    flipping to submitted, by design (approved correction, see this file's
    module docstring)."""
    agen = get_public_link_db(submitted_link["raw_token"])
    with pytest.raises(HTTPException) as exc_info:
        await agen.__anext__()
    assert exc_info.value.status_code == 404
    await agen.aclose()

    session_token = encode_intake_session_jwt(
        uuid.UUID(submitted_link["id"]), "respondent@example.com"
    )
    agen = get_public_session_db(f"Bearer {session_token}")
    session, link = await agen.__anext__()
    try:
        assert link.status == "submitted"
    finally:
        await agen.aclose()


# --- 6: one-way trigger, re-asserted as part of the closing gate -----------


def test_one_way_trigger_blocks_second_update_even_via_table_owner(
    owner_conn, org_a_id, org_a_deal_id, user_a_id, test_org_id
):
    """Re-run of tests/test_intake_link_rls.py's own test, as part of the
    closing gate -- a first UPDATE (pending -> submitted) succeeds, a second
    UPDATE on a granted column is rejected by
    trg_deal_intake_link_one_way_status, even as doadmin. A few duplicated
    lines here is fine, same reasoning as item 3 above."""
    run_id = uuid.uuid4().hex[:8]
    with owner_conn.cursor() as cur:
        link_id = _insert_link(
            cur,
            org_pk=org_a_id,
            clerk_org_id=test_org_id,
            deal_id=org_a_deal_id,
            user_pk=user_a_id,
            token_hash=_token_hash(f"one-way-gate-{run_id}"),
            expires_at=_EXPIRES_AT,
        )

        cur.execute(
            "UPDATE deal_intake_link SET status = 'submitted', submitted_at = now() WHERE id = %s",
            (link_id,),
        )

        with pytest.raises(Exception, match="status is final once left pending"):
            cur.execute(
                "UPDATE deal_intake_link SET failed_attempts = failed_attempts + 1 WHERE id = %s",
                (link_id,),
            )


# --- 7: role-boundary layer, re-run as a closing sanity pass ---------------


@pytest.mark.parametrize(
    "table_name",
    ["deals", "mandates", "screening_result", "analysis_run", "users"],
)
def test_dd_public_denied_on_out_of_scope_tables(dd_public_conn, table_name):
    """Re-run of tests/test_dd_public_grant_matrix.py's own parametrized
    check, as part of the closing gate."""
    with (
        dd_public_conn.cursor() as cur,
        pytest.raises(psycopg2.errors.InsufficientPrivilege, match="permission denied"),
    ):
        cur.execute(f"SELECT 1 FROM {table_name}")  # table_name: fixed parametrize list, not input
    dd_public_conn.rollback()


async def test_dd_app_session_keyhole_gucs_have_no_effect(db_session):
    """Re-run of tests/test_public_intake_pool.py's
    test_dd_app_session_keyhole_guc_has_no_effect, as part of the closing
    gate -- a pollution-proof before/after row-set comparison, not a naive
    "assert zero rows" (that test's own P1-07 status-file note explains why
    the naive version fails against this dev volume's accumulated fixture
    data: several P1-01 fixtures deliberately leave org_a rows behind with
    no teardown)."""
    before = await db_session.execute(text("SELECT id FROM deal_intake_link"))
    before_ids = {row[0] for row in before.fetchall()}

    await db_session.execute(
        text("SELECT set_config('app.intake_token_hash', :h, true)"), {"h": "whatever"}
    )
    after_token = await db_session.execute(text("SELECT id FROM deal_intake_link"))
    assert {row[0] for row in after_token.fetchall()} == before_ids

    await db_session.execute(
        text("SELECT set_config('app.intake_link_id', :v, true)"), {"v": str(uuid.uuid4())}
    )
    after_link = await db_session.execute(text("SELECT id FROM deal_intake_link"))
    assert {row[0] for row in after_link.fetchall()} == before_ids
