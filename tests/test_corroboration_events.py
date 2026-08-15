import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


def _insert_deal(cur, org_pk: int) -> str:
    """Reuses an existing deal for this org if one exists (org_a_id is a
    reused, ON CONFLICT DO NOTHING test org), else creates one -- claims.deal_id
    is a required FK now."""
    cur.execute("SELECT id FROM deals WHERE org_id = %s LIMIT 1", (org_pk,))
    row = cur.fetchone()
    if row is not None:
        return str(row[0])
    cur.execute(
        "INSERT INTO deals (org_id, name) VALUES (%s, 'Test Deal') RETURNING id",
        (org_pk,),
    )
    return str(cur.fetchone()[0])


def _insert_claim(cur, org_pk: int) -> str:
    """Minimal valid claims row (satisfies the CHECK constraints in
    60a151dd80b0) for corroboration_events.claim_id to point at."""
    deal_pk = _insert_deal(cur, org_pk)
    cur.execute(
        "INSERT INTO claims (org_id, deal_id, entity, attribute, value, kind, page, status) "
        "VALUES (%s, %s, 'Test Entity', 'test_attr', '{}'::jsonb, 'pdf', 1, 'missing') "
        "RETURNING id",
        (org_pk, deal_pk),
    )
    return str(cur.fetchone()[0])


@pytest.fixture
def org_a_id(owner_conn, test_org_id) -> int:
    """The organisation backing the test session's own app.org_id."""
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (clerk_org_id) DO NOTHING",
            (test_org_id, "Org A"),
        )
        cur.execute("SELECT id FROM organisation WHERE clerk_org_id = %s", (test_org_id,))
        return cur.fetchone()[0]


@pytest.fixture
def org_a_claim_id(owner_conn, org_a_id) -> str:
    with owner_conn.cursor() as cur:
        return _insert_claim(cur, org_a_id)


@pytest.fixture
def org_b_event_id(owner_conn) -> Iterator[str]:
    """A corroboration event belonging to a *different* org, seeded via the
    doadmin connection (bypasses RLS) — a dd_app session scoped to org A's
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
        claim_id = _insert_claim(cur, org_b_pk)
        cur.execute(
            "INSERT INTO corroboration_events (org_id, claim_id, outside_source, result) "
            "VALUES (%s, %s, %s, %s::jsonb) RETURNING id",
            (org_b_pk, claim_id, "ofac_screen", '{"match": false}'),
        )
        event_id = cur.fetchone()[0]

    yield str(event_id)

    with owner_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM corroboration_events WHERE id = %s", (event_id,)
        )  # doadmin only — dd_app can never do this, see immutability tests below
        cur.execute("DELETE FROM claims WHERE id = %s", (claim_id,))
        cur.execute("DELETE FROM deals WHERE org_id = %s", (org_b_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_b_pk,))


async def test_org_isolation_hides_other_org_event(db_session, org_a_id, org_b_event_id):
    result = await db_session.execute(
        text("SELECT id FROM corroboration_events WHERE id = :id"), {"id": org_b_event_id}
    )
    assert result.first() is None

    all_rows = await db_session.execute(text("SELECT id FROM corroboration_events"))
    assert all(str(row[0]) != org_b_event_id for row in all_rows.fetchall())


async def test_org_isolation_still_shows_own_org_event(
    db_session, org_a_id, org_a_claim_id, org_b_event_id
):
    insert_result = await db_session.execute(
        text(
            "INSERT INTO corroboration_events (org_id, claim_id, outside_source, result) "
            "VALUES (:org_id, :claim_id, :outside_source, :result) RETURNING id"
        ),
        {
            "org_id": org_a_id,
            "claim_id": org_a_claim_id,
            "outside_source": "entity_lookup",
            "result": '{"matched_entity": "Acme Corp"}',
        },
    )
    own_event_id = str(insert_result.scalar())
    await db_session.flush()

    fetched = await db_session.execute(
        text("SELECT outside_source FROM corroboration_events WHERE id = :id"),
        {"id": own_event_id},
    )
    row = fetched.first()
    assert row is not None
    assert row[0] == "entity_lookup"

    all_rows = await db_session.execute(text("SELECT id FROM corroboration_events"))
    ids = [str(r[0]) for r in all_rows.fetchall()]
    assert own_event_id in ids
    assert org_b_event_id not in ids


async def test_dd_app_cannot_update_corroboration_events(db_session, org_a_id, org_a_claim_id):
    await db_session.execute(
        text(
            "INSERT INTO corroboration_events (org_id, claim_id, outside_source, result) "
            "VALUES (:org_id, :claim_id, 'ofac_screen', '{}'::jsonb)"
        ),
        {"org_id": org_a_id, "claim_id": org_a_claim_id},
    )
    await db_session.flush()

    # REVOKE UPDATE ON corroboration_events FROM dd_app (see the migration
    # that creates this table) — must fail at the database, not be caught by
    # any application-level guard.
    with pytest.raises(DBAPIError, match="permission denied"):
        await db_session.execute(
            text("UPDATE corroboration_events SET outside_source = 'tampered'")
        )


async def test_dd_app_cannot_delete_corroboration_events(db_session, org_a_id, org_a_claim_id):
    await db_session.execute(
        text(
            "INSERT INTO corroboration_events (org_id, claim_id, outside_source, result) "
            "VALUES (:org_id, :claim_id, 'ofac_screen', '{}'::jsonb)"
        ),
        {"org_id": org_a_id, "claim_id": org_a_claim_id},
    )
    await db_session.flush()

    # REVOKE DELETE ON corroboration_events FROM dd_app — same guarantee.
    with pytest.raises(DBAPIError, match="permission denied"):
        await db_session.execute(text("DELETE FROM corroboration_events"))
