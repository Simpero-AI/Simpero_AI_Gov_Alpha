"""SIM-420: the resolved_entity table's own guarantees, plus the load path
corroboration adapters reach it through.

Same split as entity_resolution: tests/test_resolved_entity_fold.py covers the
artifact's judgment; this file covers the store and the seam -- RLS isolation,
write-once enforcement, the has-an-anchor CHECK, and the once-per-deal load
that keeps a pass over N claims x M sources from re-reading the same row N x M
times.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.models.resolved_entity import REGISTRY_CIK, REGISTRY_ISED_CORPORATION_ID
from app.repo.EntityResolutionRepo import EntityResolutionRepo
from app.repo.ResolvedEntityRepo import ResolvedEntityRepo
from app.services.entity_resolution import resolved as resolved_module
from app.services.entity_resolution.resolved import (
    load_resolved_entity,
    record_resolved_entity,
)
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
            (org_pk, "Resolved Entity Test Deal"),
        )
        return cur.fetchone()[0]


@pytest.fixture
def second_deal_pk(owner_conn, org_pk) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (org_pk, "Resolved Entity Second Deal"),
        )
        return cur.fetchone()[0]


def _resolved(cik: str = "0000000042", legal_name: str = "Acme Technologies Ltd.") -> Resolution:
    return Resolution(
        status="resolved",
        source="sec_edgar",
        query_name="Acme",
        registry_id=cik,
        legal_name=legal_name,
        former_names=(FormerName(name="Acme Holdings Ltd", from_date="1999-01-01"),),
        matched_on="current_name",
        evidence={"candidates": 1},
    )


def _not_found() -> Resolution:
    return Resolution(
        status="not_found",
        source="sec_edgar",
        query_name="Acme",
        reason="No SEC filer matches this name.",
        evidence={"candidates": 0},
    )


# --------------------------------------------------------------------------
# record_resolved_entity: the fold, persisted.
# --------------------------------------------------------------------------


async def test_recording_persists_the_folded_identity(db_session, org_pk, deal_pk) -> None:
    await EntityResolutionRepo(db_session).record(_resolved(), org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()

    entity = await record_resolved_entity(db_session, org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()

    assert entity is not None
    assert entity.canonical_name == "Acme Technologies Ltd."
    assert entity.aliases == ("Acme Holdings Ltd",)
    assert entity.registry_id(REGISTRY_CIK) == "0000000042"

    row = await ResolvedEntityRepo(db_session).latest_for_deal(deal_pk)
    assert row is not None
    assert row.canonical_name == "Acme Technologies Ltd."
    assert row.aliases == ["Acme Holdings Ltd"]
    assert row.registry_ids == {REGISTRY_CIK: "0000000042"}


async def test_the_evidence_names_the_rows_the_fold_read(db_session, org_pk, deal_pk) -> None:
    """A reader must be able to retrace the fold without re-querying."""
    source_row = await EntityResolutionRepo(db_session).record(
        _resolved(), org_id=org_pk, deal_id=deal_pk
    )
    await db_session.flush()

    await record_resolved_entity(db_session, org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()

    row = await ResolvedEntityRepo(db_session).latest_for_deal(deal_pk)
    assert row is not None
    folded_from = row.evidence["folded_from"]
    assert [f["entity_resolution_id"] for f in folded_from] == [str(source_row.id)]
    assert folded_from[0]["source"] == "sec_edgar"
    assert folded_from[0]["status"] == "resolved"


async def test_a_deal_nothing_resolved_writes_no_row_at_all(db_session, org_pk, deal_pk) -> None:
    """The clean no-signal path, at the storage layer: "we looked and SEC has
    nothing" is a real entity_resolution row, but it must leave NO
    resolved_entity row -- an adapter reading one would treat a name with no
    anchor as an identity."""
    await EntityResolutionRepo(db_session).record(_not_found(), org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()

    assert await record_resolved_entity(db_session, org_id=org_pk, deal_id=deal_pk) is None
    await db_session.flush()

    count = await db_session.scalar(
        text("SELECT count(*) FROM resolved_entity WHERE deal_id = :d"), {"d": deal_pk}
    )
    assert count == 0


async def test_a_deal_never_resolved_at_all_writes_no_row(db_session, org_pk, deal_pk) -> None:
    assert await record_resolved_entity(db_session, org_id=org_pk, deal_id=deal_pk) is None


async def test_re_folding_appends_rather_than_replacing(db_session, org_pk, deal_pk) -> None:
    """Append-only: two callers racing on one deal append two rows rather than
    losing each other's write, and latest_for_deal picks the newer
    deterministically."""
    repo = EntityResolutionRepo(db_session)
    await repo.record(_resolved(cik="0000000001"), org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()
    await record_resolved_entity(db_session, org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()

    await repo.record(
        _resolved(cik="0000000042", legal_name="Acme Technologies Inc."),
        org_id=org_pk,
        deal_id=deal_pk,
    )
    await db_session.flush()
    await record_resolved_entity(db_session, org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()

    count = await db_session.scalar(
        text("SELECT count(*) FROM resolved_entity WHERE deal_id = :d"), {"d": deal_pk}
    )
    assert count == 2

    latest = await ResolvedEntityRepo(db_session).latest_for_deal(deal_pk)
    assert latest is not None
    assert latest.registry_ids == {REGISTRY_CIK: "0000000042"}


async def test_the_newest_attempt_per_registry_is_what_gets_folded(
    db_session, org_pk, deal_pk
) -> None:
    """latest_per_source_for_deal, end to end: a superseded lookup must not
    win over the one that replaced it."""
    repo = EntityResolutionRepo(db_session)
    await repo.record(_resolved(cik="0000000001"), org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()
    await repo.record(_resolved(cik="0000000042"), org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()

    rows = await repo.latest_per_source_for_deal(deal_pk)
    assert len(rows) == 1
    assert rows[0].registry_id == "0000000042"


# --------------------------------------------------------------------------
# load_resolved_entity: the seam adapters reach the artifact through.
# --------------------------------------------------------------------------


async def test_load_returns_none_for_a_deal_with_no_artifact(db_session, deal_pk) -> None:
    assert await load_resolved_entity(db_session, deal_pk) is None


async def test_load_round_trips_the_stored_artifact(db_session, org_pk, deal_pk) -> None:
    """The AC in one test: reachable from `db` + `claim.deal_id` alone, which
    is what makes a CorroborationSource protocol change unnecessary."""
    await EntityResolutionRepo(db_session).record(_resolved(), org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()
    await record_resolved_entity(db_session, org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()
    db_session.info.pop("sim420_resolved_entity_by_deal", None)  # force a real read

    entity = await load_resolved_entity(db_session, deal_pk)

    assert entity is not None
    assert entity.deal_id == uuid.UUID(str(deal_pk))
    assert entity.canonical_name == "Acme Technologies Ltd."
    assert entity.matches("ACME HOLDINGS LTD") == "Acme Holdings Ltd"
    assert entity.registry_id(REGISTRY_CIK) == "0000000042"
    assert entity.registry_id(REGISTRY_ISED_CORPORATION_ID) is None


async def test_the_artifact_is_loaded_once_per_deal_not_once_per_call(
    db_session, org_pk, deal_pk, monkeypatch
) -> None:
    """Resolution runs once per deal, not per (claim x source). Without this,
    a pass over 200 claims and 4 sources issues 800 identical queries."""
    await EntityResolutionRepo(db_session).record(_resolved(), org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()
    await record_resolved_entity(db_session, org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()
    db_session.info.pop("sim420_resolved_entity_by_deal", None)

    reads = 0
    original = ResolvedEntityRepo.latest_for_deal

    async def counting(self: Any, deal_id: Any) -> Any:
        nonlocal reads
        reads += 1
        return await original(self, deal_id)

    monkeypatch.setattr(ResolvedEntityRepo, "latest_for_deal", counting)

    for _ in range(5):
        assert await load_resolved_entity(db_session, deal_pk) is not None

    assert reads == 1


async def test_the_no_signal_answer_is_cached_too(db_session, org_pk, deal_pk, monkeypatch) -> None:
    """The common case in the target book is a company no registry resolved.
    If only the hit were cached, that case would cost a query per claim per
    adapter -- the exact opposite of the intended saving."""
    reads = 0
    original = ResolvedEntityRepo.latest_for_deal

    async def counting(self: Any, deal_id: Any) -> Any:
        nonlocal reads
        reads += 1
        return await original(self, deal_id)

    monkeypatch.setattr(ResolvedEntityRepo, "latest_for_deal", counting)

    for _ in range(5):
        assert await load_resolved_entity(db_session, deal_pk) is None

    assert reads == 1


async def test_two_deals_do_not_share_one_cache_entry(
    db_session, org_pk, deal_pk, second_deal_pk
) -> None:
    """The memo is keyed by deal, so a second deal in the same pass gets its
    own answer rather than the first deal's identity."""
    await EntityResolutionRepo(db_session).record(_resolved(), org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()
    await record_resolved_entity(db_session, org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()

    assert await load_resolved_entity(db_session, deal_pk) is not None
    assert await load_resolved_entity(db_session, second_deal_pk) is None


async def test_recording_primes_the_cache_with_its_own_write(
    db_session, org_pk, deal_pk, monkeypatch
) -> None:
    """A caller that records and then runs the corroboration pass on the same
    session must not re-read its own uncommitted write."""
    await EntityResolutionRepo(db_session).record(_resolved(), org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()
    await record_resolved_entity(db_session, org_id=org_pk, deal_id=deal_pk)

    async def explode(self: Any, deal_id: Any) -> Any:
        raise AssertionError("load_resolved_entity re-read a freshly recorded fold")

    monkeypatch.setattr(ResolvedEntityRepo, "latest_for_deal", explode)

    entity = await load_resolved_entity(db_session, deal_pk)
    assert entity is not None
    assert entity.canonical_name == "Acme Technologies Ltd."


def test_the_cache_key_is_namespaced_to_this_module() -> None:
    """`AsyncSession.info` is shared with anything else that uses it, so the
    key has to be unmistakably ours."""
    assert resolved_module._CACHE_KEY.startswith("sim420_")


# --------------------------------------------------------------------------
# The table's own invariants.
# --------------------------------------------------------------------------


async def test_a_row_with_no_registry_id_violates_the_check(db_session, org_pk, deal_pk) -> None:
    """The service refuses to write one, but the database is the backstop: an
    anchorless "identity" is indistinguishable from a guess."""
    with pytest.raises(DBAPIError, match="ck_resolved_entity_has_registry_id"):
        await db_session.execute(
            text(
                "INSERT INTO resolved_entity "
                "(org_id, deal_id, canonical_name, aliases, registry_ids, evidence) "
                "VALUES (:org, :deal, 'Acme', '[]'::jsonb, '{}'::jsonb, '{}'::jsonb)"
            ),
            {"org": org_pk, "deal": deal_pk},
        )


async def test_a_blank_canonical_name_violates_the_check(db_session, org_pk, deal_pk) -> None:
    with pytest.raises(DBAPIError, match="ck_resolved_entity_has_name"):
        await db_session.execute(
            text(
                "INSERT INTO resolved_entity "
                "(org_id, deal_id, canonical_name, aliases, registry_ids, evidence) "
                "VALUES (:org, :deal, '   ', '[]'::jsonb, '{\"cik\": \"1\"}'::jsonb, '{}'::jsonb)"
            ),
            {"org": org_pk, "deal": deal_pk},
        )


async def test_aliases_must_be_a_json_array(db_session, org_pk, deal_pk) -> None:
    with pytest.raises(DBAPIError, match="ck_resolved_entity_aliases_array"):
        await db_session.execute(
            text(
                "INSERT INTO resolved_entity "
                "(org_id, deal_id, canonical_name, aliases, registry_ids, evidence) "
                "VALUES (:org, :deal, 'Acme', '\"nope\"'::jsonb, "
                "'{\"cik\": \"1\"}'::jsonb, '{}'::jsonb)"
            ),
            {"org": org_pk, "deal": deal_pk},
        )


async def test_registry_ids_must_be_a_json_object(db_session, org_pk, deal_pk) -> None:
    with pytest.raises(DBAPIError, match="ck_resolved_entity_registry_ids_object"):
        await db_session.execute(
            text(
                "INSERT INTO resolved_entity "
                "(org_id, deal_id, canonical_name, aliases, registry_ids, evidence) "
                "VALUES (:org, :deal, 'Acme', '[]'::jsonb, '[\"cik\"]'::jsonb, '{}'::jsonb)"
            ),
            {"org": org_pk, "deal": deal_pk},
        )


# --------------------------------------------------------------------------
# Append-only, at the database layer.
# --------------------------------------------------------------------------


async def _seed_row(db_session, org_pk, deal_pk) -> None:
    await EntityResolutionRepo(db_session).record(_resolved(), org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()
    await record_resolved_entity(db_session, org_id=org_pk, deal_id=deal_pk)
    await db_session.flush()


async def test_update_is_denied(db_session, org_pk, deal_pk) -> None:
    """Every corroboration event on this deal inherits this identity, so a
    mutable row would silently re-point old events at a different company."""
    await _seed_row(db_session, org_pk, deal_pk)

    with pytest.raises(DBAPIError, match="permission denied"):
        await db_session.execute(text("UPDATE resolved_entity SET canonical_name = 'Other Co'"))


async def test_delete_is_denied(db_session, org_pk, deal_pk) -> None:
    await _seed_row(db_session, org_pk, deal_pk)

    with pytest.raises(DBAPIError, match="permission denied"):
        await db_session.execute(text("DELETE FROM resolved_entity"))


# --------------------------------------------------------------------------
# Tenant isolation.
# --------------------------------------------------------------------------


@pytest.fixture
def org_b_row(owner_conn) -> Any:
    """A whole second tenant with a resolved_entity row, seeded as doadmin so
    RLS never sees it being created. Torn down in FK order."""
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
            "INSERT INTO resolved_entity "
            "(org_id, deal_id, canonical_name, aliases, registry_ids, evidence) "
            "VALUES (%s, %s, 'Org B Co', '[]'::jsonb, '{\"cik\": \"0000000777\"}'::jsonb, "
            "'{}'::jsonb) RETURNING id",
            (other_org, other_deal),
        )
        row_id = cur.fetchone()[0]

    yield row_id, other_deal

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM resolved_entity WHERE org_id = %s", (other_org,))
        cur.execute("DELETE FROM deals WHERE org_id = %s", (other_org,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (other_org,))


async def test_another_orgs_artifact_is_invisible(db_session, org_b_row) -> None:
    """A deal's resolved identity is exactly the kind of thing that must not
    leak across tenants."""
    row_id, _ = org_b_row
    found = await db_session.scalar(
        text("SELECT count(*) FROM resolved_entity WHERE id = :i"), {"i": row_id}
    )
    assert found == 0


async def test_loading_another_orgs_deal_is_no_signal_not_their_identity(
    db_session, org_b_row
) -> None:
    """The load path inherits RLS rather than working around it -- a
    cross-tenant deal_id reads as "nothing resolved", never as their company."""
    _, other_deal = org_b_row
    assert await load_resolved_entity(db_session, other_deal) is None


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
                "INSERT INTO resolved_entity "
                "(org_id, deal_id, canonical_name, aliases, registry_ids, evidence) "
                "VALUES (:org, :deal, 'Acme', '[]'::jsonb, '{\"cik\": \"1\"}'::jsonb, '{}'::jsonb)"
            ),
            {"org": foreign_org, "deal": deal_pk},
        )

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM organisation WHERE id = %s", (foreign_org,))
