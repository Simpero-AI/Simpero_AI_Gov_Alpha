"""entity_resolution: the table's own guarantees -- RLS isolation, write-once
enforcement, the resolved/registry_id invariant, and latest_for_deal ordering.

Same split as corroboration: tests/test_entity_resolution_edgar.py covers the
adapter's judgment; this file covers the store the anchor lands in. The
guarantees here are what let SIM-408/253/254 trust the anchor they read.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.repo.EntityResolutionRepo import EntityResolutionRepo
from app.services.entity_resolution.types import FormerName, Resolution


@pytest.fixture
def org_pk(owner_conn, test_org_id) -> int:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (clerk_org_id) DO NOTHING",
            (test_org_id, "Org A"),
        )
        cur.execute("SELECT id FROM organisation WHERE clerk_org_id = %s", (test_org_id,))
        return cur.fetchone()[0]


@pytest.fixture
def deal_pk(owner_conn, org_pk) -> str:
    """Seeded via owner_conn (bypassing RLS) so the FK resolves regardless of
    the RLS scope the async session runs under."""
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (org_pk, "Entity Resolution Test Deal"),
        )
        return cur.fetchone()[0]


def _resolved(name: str = "Acme Corp", cik: str = "0000000042") -> Resolution:
    return Resolution(
        status="resolved",
        source="sec_edgar",
        query_name=name,
        registry_id=cik,
        legal_name="ACME INC",
        former_names=(
            FormerName(name="Acme Holdings", from_date="1999-01-01", to_date="2005-02-02"),
        ),
        matched_on="current_name",
        evidence={"normalized_query": "ACME", "candidates": 1},
    )


def _not_found(name: str = "Nobody Ltd") -> Resolution:
    return Resolution(
        status="not_found",
        source="sec_edgar",
        query_name=name,
        reason="No SEC filer matches this name.",
        evidence={"normalized_query": "NOBODY", "candidates": 0},
    )


# --------------------------------------------------------------------------
# The dataclass invariant, before anything reaches the database.
# --------------------------------------------------------------------------


def test_resolved_without_a_registry_id_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="registry_id"):
        Resolution(status="resolved", source="sec_edgar", query_name="Acme")


def test_a_non_resolved_status_cannot_carry_a_registry_id() -> None:
    """An anchor on a not_found would be an answer we explicitly declined to
    give."""
    with pytest.raises(ValueError, match="must not carry a registry_id"):
        Resolution(
            status="not_found", source="sec_edgar", query_name="Acme", registry_id="0000000042"
        )


# --------------------------------------------------------------------------
# Persistence.
# --------------------------------------------------------------------------


async def test_record_persists_every_field(db_session, org_pk, deal_pk) -> None:
    row = await EntityResolutionRepo(db_session).record(_resolved(), org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()

    assert row.status == "resolved"
    assert row.registry_id == "0000000042"
    assert row.legal_name == "ACME INC"
    assert row.matched_on == "current_name"
    assert row.source == "sec_edgar"
    # query_name is the name as SEARCHED -- deals.name is mutable, so without
    # it the row could only say what the deal is called now.
    assert row.query_name == "Acme Corp"
    assert row.former_names == [{"name": "Acme Holdings", "from": "1999-01-01", "to": "2005-02-02"}]


async def test_not_found_is_persisted_as_a_real_answer(db_session, org_pk, deal_pk) -> None:
    """Absence is not contradiction, but it IS a result worth recording: the
    row proves we looked."""
    row = await EntityResolutionRepo(db_session).record(
        _not_found(), org_id=org_pk, deal_id=deal_pk
    )
    await db_session.flush()

    assert row.status == "not_found"
    assert row.registry_id is None
    assert row.reason is not None


async def test_resolved_with_null_registry_id_violates_the_check(
    db_session, org_pk, deal_pk
) -> None:
    """The dataclass guards this too, but the database is the backstop -- a
    future writer that bypasses Resolution must still not land an anchorless
    resolve."""
    with pytest.raises(DBAPIError, match="ck_entity_resolution_resolved_requires_registry_id"):
        await db_session.execute(
            text(
                "INSERT INTO entity_resolution "
                "(org_id, deal_id, source, status, query_name, evidence) "
                "VALUES (:org, :deal, 'sec_edgar', 'resolved', 'Acme', '{}'::jsonb)"
            ),
            {"org": org_pk, "deal": deal_pk},
        )


async def test_unknown_status_violates_the_check(db_session, org_pk, deal_pk) -> None:
    with pytest.raises(DBAPIError, match="ck_entity_resolution_status"):
        await db_session.execute(
            text(
                "INSERT INTO entity_resolution "
                "(org_id, deal_id, source, status, query_name, evidence) "
                "VALUES (:org, :deal, 'sec_edgar', 'probably', 'Acme', '{}'::jsonb)"
            ),
            {"org": org_pk, "deal": deal_pk},
        )


async def test_unknown_source_violates_the_check(db_session, org_pk, deal_pk) -> None:
    """A second registry needs a migration widening this CHECK, so a typo'd
    or unregistered source cannot quietly enter the record."""
    with pytest.raises(DBAPIError, match="ck_entity_resolution_source"):
        await db_session.execute(
            text(
                "INSERT INTO entity_resolution "
                "(org_id, deal_id, source, status, query_name, evidence) "
                "VALUES (:org, :deal, 'opencorporates', 'not_found', 'Acme', '{}'::jsonb)"
            ),
            {"org": org_pk, "deal": deal_pk},
        )


# --------------------------------------------------------------------------
# Append-only, at the database layer.
# --------------------------------------------------------------------------


async def test_update_is_denied(db_session, org_pk, deal_pk) -> None:
    await EntityResolutionRepo(db_session).record(_resolved(), org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()

    with pytest.raises(DBAPIError, match="permission denied"):
        await db_session.execute(text("UPDATE entity_resolution SET registry_id = '0000000001'"))


async def test_delete_is_denied(db_session, org_pk, deal_pk) -> None:
    await EntityResolutionRepo(db_session).record(_resolved(), org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()

    with pytest.raises(DBAPIError, match="permission denied"):
        await db_session.execute(text("DELETE FROM entity_resolution"))


# --------------------------------------------------------------------------
# latest_for_deal.
# --------------------------------------------------------------------------


async def test_latest_for_deal_returns_the_newest_of_two_rows_in_one_transaction(
    db_session, org_pk, deal_pk
) -> None:
    """The reason created_at is clock_timestamp() and not now(): now() is
    constant across a transaction, so these two would tie and "latest" would
    pick arbitrarily between them."""
    repo = EntityResolutionRepo(db_session)
    await repo.record(_not_found(), org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()
    second = await repo.record(_resolved(), org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()

    latest = await repo.latest_for_deal(deal_pk)

    assert latest is not None
    assert latest.id == second.id
    assert latest.status == "resolved"


async def test_latest_for_deal_is_none_when_never_resolved(db_session, deal_pk) -> None:
    assert await EntityResolutionRepo(db_session).latest_for_deal(deal_pk) is None


async def test_re_resolving_appends_rather_than_replacing(db_session, org_pk, deal_pk) -> None:
    """A company that was not_found before it filed, then resolved after. Both
    rows survive -- the history is the record of how the answer changed."""
    repo = EntityResolutionRepo(db_session)
    await repo.record(_not_found(), org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()
    await repo.record(_resolved(), org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()

    count = await db_session.scalar(
        text("SELECT count(*) FROM entity_resolution WHERE deal_id = :d"), {"d": deal_pk}
    )
    assert count == 2


# --------------------------------------------------------------------------
# Tenant isolation.
# --------------------------------------------------------------------------


@pytest.fixture
def org_b_row(owner_conn) -> Any:
    """A whole second tenant with a resolution row, seeded as doadmin so RLS
    never sees it being created. Torn down in FK order."""
    clerk_org_id = f"other-tenant-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) "
            "VALUES (%s, %s, now()) RETURNING id",
            (clerk_org_id, "Org B"),
        )
        other_org = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (other_org, "Org B Deal"),
        )
        other_deal = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO entity_resolution "
            "(org_id, deal_id, source, status, query_name, registry_id, evidence) "
            "VALUES (%s, %s, 'sec_edgar', 'resolved', 'Org B Co', '0000000777', '{}'::jsonb) "
            "RETURNING id",
            (other_org, other_deal),
        )
        row_id = cur.fetchone()[0]

    yield row_id

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM entity_resolution WHERE org_id = %s", (other_org,))
        cur.execute("DELETE FROM deals WHERE org_id = %s", (other_org,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (other_org,))


async def test_another_orgs_resolution_is_invisible(db_session, org_b_row) -> None:
    """A deal's resolved identity is exactly the kind of thing that must not
    leak across tenants."""
    found = await db_session.scalar(
        text("SELECT count(*) FROM entity_resolution WHERE id = :i"), {"i": org_b_row}
    )
    assert found == 0


async def test_own_org_resolution_is_visible(db_session, org_pk, deal_pk, org_b_row) -> None:
    row = await EntityResolutionRepo(db_session).record(_resolved(), org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()

    found = await db_session.scalar(
        text("SELECT count(*) FROM entity_resolution WHERE id = :i"), {"i": row.id}
    )
    assert found == 1


async def test_writing_a_row_for_another_org_is_blocked(db_session, deal_pk, owner_conn) -> None:
    """RLS is FORCEd, so the policy applies to INSERT too -- a row cannot be
    planted under someone else's org_id."""
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) "
            "VALUES (%s, %s, now()) RETURNING id",
            (f"foreign-{uuid.uuid4().hex[:8]}", "Foreign Org"),
        )
        foreign_org = cur.fetchone()[0]

    with pytest.raises((DBAPIError, IntegrityError), match="row-level security"):
        await db_session.execute(
            text(
                "INSERT INTO entity_resolution "
                "(org_id, deal_id, source, status, query_name, evidence) "
                "VALUES (:org, :deal, 'sec_edgar', 'not_found', 'Acme', '{}'::jsonb)"
            ),
            {"org": foreign_org, "deal": deal_pk},
        )

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM organisation WHERE id = %s", (foreign_org,))
