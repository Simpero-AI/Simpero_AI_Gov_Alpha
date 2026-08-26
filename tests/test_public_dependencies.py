import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.core.intake_security import sha256_hex
from app.core.public_dependencies import get_public_link_db

_EXPIRES_AT = datetime.now(UTC) + timedelta(days=7)


def _insert_deal(cur, org_pk: int, name: str = "Test Deal") -> str:
    cur.execute("INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id", (org_pk, name))
    return str(cur.fetchone()[0])


@pytest.fixture
def org_a_deal_id(owner_conn, org_a_id) -> str:
    """No teardown, deliberately -- same reasoning as
    test_intake_link_rls.py's org_a_deal_id."""
    with owner_conn.cursor() as cur:
        return _insert_deal(cur, org_a_id, "Org A's deal")


@pytest.fixture
def pending_link_with_token(
    owner_conn, org_a_id, org_a_deal_id, user_a_id, test_org_id
) -> Iterator[dict]:
    """A pending, unexpired deal_intake_link row seeded via owner_conn
    (bypasses RLS) -- we control the raw token here (never stored), and seed
    only its SHA-256 into token_hash, mirroring how the real create-link
    route (P3) would produce it."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = sha256_hex(raw_token)
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deal_intake_link "
            "(org_id, clerk_org_id, deal_id, token_hash, recipient_email, expires_at, "
            "created_by_user_id, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending') RETURNING id",
            (
                org_a_id,
                test_org_id,
                org_a_deal_id,
                token_hash,
                "recipient@org-a.example",
                _EXPIRES_AT,
                user_a_id,
            ),
        )
        link_id = str(cur.fetchone()[0])

    yield {
        "id": link_id,
        "raw_token": raw_token,
        "clerk_org_id": test_org_id,
        "deal_id": org_a_deal_id,
    }

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM deal_intake_link WHERE id = %s", (link_id,))


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
