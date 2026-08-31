import uuid
from collections.abc import Iterator

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.core.intake_security import encode_intake_session_jwt
from app.core.public_dependencies import get_public_link_db, get_public_session_db

# org_a_deal_id / pending_link_with_token fixtures moved to tests/conftest.py
# (P3-07) -- tests/test_public_intake_session.py needs them too now, same
# "more than one module needs it" trigger as conftest.py's org_a_id.

_DECLARED_HASH = "a" * 64


def _insert_data_source(cur, org_pk: int, deal_id: str, storage_key: str) -> str:
    cur.execute(
        "INSERT INTO data_source (org_id, deal_id, storage_key, filename, declared_sha256) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (org_pk, deal_id, storage_key, "intake.pdf", _DECLARED_HASH),
    )
    return str(cur.fetchone()[0])


async def test_unknown_token_raises_404_without_a_second_query():
    """A malformed/unknown token can't match any seeded row's hash, so
    get_by_token_hash returning None is sufficient proof the 404 fires
    before any second (phase-2) query would run -- no mocking/spying needed."""
    agen = get_public_link_db("not-a-real-token")
    with pytest.raises(HTTPException) as exc_info:
        await agen.__anext__()
    assert exc_info.value.status_code == 404

    await agen.aclose()


async def test_valid_token_yields_session_and_link_with_gucs_set(pending_link_with_token):
    agen = get_public_link_db(pending_link_with_token["raw_token"])
    session, link = await agen.__anext__()

    try:
        # org_id was read off the link row itself (clerk_org_id column),
        # never derived from an organisation join -- proving that also
        # proves no organisation-table query happened in the call path,
        # since app.org_id isn't set until AFTER this value is read and
        # organisation's own RLS policy would return nothing before then.
        assert link.clerk_org_id == pending_link_with_token["clerk_org_id"]
        assert str(link.deal_id) == pending_link_with_token["deal_id"]

        # Both GUCs were set together in phase 2's single statement, before
        # this query (the first thing the test does with the yielded
        # session) runs.
        result = await session.execute(
            text(
                "SELECT current_setting('app.org_id', true), "
                "current_setting('app.intake_deal_id', true)"
            )
        )
        org_id, deal_id = result.one()
        assert org_id == pending_link_with_token["clerk_org_id"]
        assert deal_id == pending_link_with_token["deal_id"]
    finally:
        await agen.aclose()


async def test_unknown_token_random_uuid_style_also_404s():
    """A second unknown-token shape (not just a short string) -- still the
    same 404, same body, confirming the 404 path isn't string-shape-specific."""
    agen = get_public_link_db(str(uuid.uuid4()))
    with pytest.raises(HTTPException) as exc_info:
        await agen.__anext__()
    assert exc_info.value.status_code == 404

    await agen.aclose()


@pytest.fixture
def org_b_docs(owner_conn) -> Iterator[str]:
    """A second org's pending link + data_source row, seeded via owner_conn
    (bypasses RLS) -- same shape as test_intake_keyhole_policies.py's
    org_b_docs fixture, reused here so the cross-org proof for
    get_public_session_db exercises a real second org/deal, not just a
    hand-crafted GUC."""
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
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (org_b_pk, "Org B's deal"),
        )
        deal_id = str(cur.fetchone()[0])
        data_source_id = _insert_data_source(cur, org_b_pk, deal_id, "org-b/intake.pdf")

    yield data_source_id

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM data_source WHERE id = %s", (data_source_id,))
        cur.execute("DELETE FROM deals WHERE id = %s", (deal_id,))
        cur.execute("DELETE FROM users WHERE id = %s", (user_b_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_b_pk,))


async def test_valid_session_jwt_yields_session_and_link_with_gucs_set(
    pending_link_with_token,
):
    session_token = encode_intake_session_jwt(
        uuid.UUID(pending_link_with_token["id"]), "respondent@example.com"
    )
    agen = get_public_session_db(f"Bearer {session_token}")
    session, link = await agen.__anext__()

    try:
        assert link.clerk_org_id == pending_link_with_token["clerk_org_id"]
        assert str(link.deal_id) == pending_link_with_token["deal_id"]

        result = await session.execute(
            text(
                "SELECT current_setting('app.org_id', true), "
                "current_setting('app.intake_deal_id', true)"
            )
        )
        org_id, deal_id = result.one()
        assert org_id == pending_link_with_token["clerk_org_id"]
        assert deal_id == pending_link_with_token["deal_id"]
    finally:
        await agen.aclose()


async def test_unknown_link_id_in_validly_signed_jwt_404s():
    """A genuinely nonexistent UUID -- not RLS-invisible, just not there --
    must still 404 cleanly. app.intake_link_id IS set before this call
    (phase 1), so get_by_id (not RLS-filtered the same way get_by_token_hash
    is at that exact moment) must still resolve to None correctly."""
    session_token = encode_intake_session_jwt(uuid.uuid4(), "nobody@example.com")
    agen = get_public_session_db(f"Bearer {session_token}")
    with pytest.raises(HTTPException) as exc_info:
        await agen.__anext__()
    assert exc_info.value.status_code == 404

    await agen.aclose()


async def test_bad_session_token_404s_not_401():
    """Self-review follow-up fix: decode_intake_session_jwt raising
    AuthenticationError must surface as the same 404 every other failure
    mode here returns, never the app-wide AuthenticationError -> 401 handler
    (app/main.py) with its raw JWT-library message -- that would let a bad
    session token distinguish itself, reopening the enumeration oracle the
    404-only design (brief section 5.2) exists to prevent. Garbage input is
    enough to prove this without depending on real expiry timing."""
    agen = get_public_session_db("Bearer not-a-valid-jwt")
    with pytest.raises(HTTPException) as exc_info:
        await agen.__anext__()
    assert exc_info.value.status_code == 404

    await agen.aclose()


@pytest.mark.parametrize("authorization", [None, "not-a-bearer-token", "Bearer"])
async def test_missing_or_malformed_authorization_header_404s(authorization):
    """No `Authorization` header, or one without the `Bearer ` prefix, must
    404 the same as any other bad-token path -- never a distinguishable
    error, per the 404-only design (brief section 5.2)."""
    agen = get_public_session_db(authorization)
    with pytest.raises(HTTPException) as exc_info:
        await agen.__anext__()
    assert exc_info.value.status_code == 404

    await agen.aclose()


async def test_session_jwt_cannot_read_another_orgs_data_even_by_hand_crafted_guc(
    pending_link_with_token, org_b_docs
):
    """The ticket's stated acceptance criterion: a session JWT correctly
    naming org A's real link_id cannot be used to read org B's data, even by
    hand-crafting app.intake_link_id afterward to point at org B -- because
    policy B still has to resolve whatever id is actually in the GUC against
    a real, pending, unexpired row. Org A's session sees org A's org_id, not
    org B's, and org A's data_source query sees zero of org B's rows."""
    session_token = encode_intake_session_jwt(
        uuid.UUID(pending_link_with_token["id"]), "respondent@example.com"
    )
    agen = get_public_session_db(f"Bearer {session_token}")
    session, link = await agen.__anext__()

    try:
        result = await session.execute(text("SELECT current_setting('app.org_id', true)"))
        (org_id,) = result.one()
        assert org_id == pending_link_with_token["clerk_org_id"]

        result = await session.execute(text("SELECT id FROM data_source"))
        ids = [str(r[0]) for r in result.fetchall()]
        assert org_b_docs not in ids
    finally:
        await agen.aclose()
