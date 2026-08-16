import uuid
from collections.abc import Iterator

import pytest

from app.repo.DealRepo import DealRepo


@pytest.fixture
def org_b_deal_id(owner_conn) -> Iterator[str]:
    """A deal belonging to a *different* org, seeded via the doadmin
    connection (bypasses RLS) — a dd_app session scoped to org A's
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
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (org_b_pk, "Org B's deal"),
        )
        deal_id = cur.fetchone()[0]

    yield str(deal_id)

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM deals WHERE id = %s", (deal_id,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_b_pk,))


async def test_org_isolation_hides_other_org_deal(db_session, org_a_id, org_b_deal_id):
    repo = DealRepo(db_session)

    # A session scoped to org A cannot fetch org B's deal by id — RLS makes
    # it look like the row doesn't exist, not a permission error.
    assert await repo.get_by_id(org_b_deal_id) is None

    # ... nor does it show up in an unscoped list query. No `WHERE org_id =`
    # in the repo — RLS alone must do the filtering.
    all_deals = await repo.list()
    assert all(str(deal.id) != org_b_deal_id for deal in all_deals)


async def test_org_isolation_still_shows_own_org_deal(db_session, org_a_id, org_b_deal_id):
    repo = DealRepo(db_session)
    own_deal = await repo.create({"org_id": org_a_id, "name": "Org A's deal"})
    await db_session.flush()

    fetched = await repo.get_by_id(own_deal.id)
    assert fetched is not None
    assert fetched.name == "Org A's deal"

    all_deals = await repo.list()
    assert any(deal.id == own_deal.id for deal in all_deals)
    assert all(str(deal.id) != org_b_deal_id for deal in all_deals)
