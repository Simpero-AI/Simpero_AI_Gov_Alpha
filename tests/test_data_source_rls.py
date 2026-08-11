import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.repo.DataSourceRepo import DataSourceRepo

_DECLARED_HASH = "a" * 64
_FINGERPRINT_HASH = "b" * 64


def _insert_deal(cur, org_pk: int, name: str = "Test Deal") -> str:
    cur.execute("INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id", (org_pk, name))
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
def org_a_deal_id(owner_conn, org_a_id) -> str:
    """A deal belonging to org A -- data_source.deal_id's (real, NOT NULL) FK
    target. No teardown, deliberately, same as test_corroboration_events.py's
    org_a_claim_id: db_session's own transaction (which inserts data_source
    rows referencing this deal within the test) is only rolled back *after*
    function-scoped fixture teardown runs, so a synchronous DELETE here would
    block on the FK-reference lock db_session's still-open transaction holds
    on this row -- a deadlock, not a real correctness issue."""
    with owner_conn.cursor() as cur:
        return _insert_deal(cur, org_a_id, "Org A's deal")


@pytest.fixture
def org_b_data_source_id(owner_conn) -> Iterator[str]:
    """A data_source row belonging to a *different* org, seeded via the
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
            "INSERT INTO data_source (org_id, deal_id, storage_key, filename, declared_sha256) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (org_b_pk, deal_id, "org-b/key.pdf", "b.pdf", _DECLARED_HASH),
        )
        data_source_id = cur.fetchone()[0]

    yield str(data_source_id)

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM data_source WHERE id = %s", (data_source_id,))
        cur.execute("DELETE FROM deals WHERE id = %s", (deal_id,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_b_pk,))


async def test_org_isolation_hides_other_org_data_source(
    db_session, org_a_id, org_b_data_source_id
):
    result = await db_session.execute(
        text("SELECT id FROM data_source WHERE id = :id"), {"id": org_b_data_source_id}
    )
    assert result.first() is None

    all_rows = await db_session.execute(text("SELECT id FROM data_source"))
    assert all(str(row[0]) != org_b_data_source_id for row in all_rows.fetchall())


async def test_org_isolation_still_shows_own_org_data_source(
    db_session, org_a_id, org_a_deal_id, org_b_data_source_id
):
    repo = DataSourceRepo(db_session)
    own = await repo.create(
        {
            "org_id": org_a_id,
            "deal_id": org_a_deal_id,
            "storage_key": "org-a/key.pdf",
            "filename": "a.pdf",
            "declared_sha256": _DECLARED_HASH,
        }
    )
    await db_session.flush()

    fetched = await repo.get_by_id(own.id)
    assert fetched is not None
    assert fetched.filename == "a.pdf"

    all_rows = await db_session.execute(text("SELECT id FROM data_source"))
    ids = [str(r[0]) for r in all_rows.fetchall()]
    assert str(own.id) in ids
    assert org_b_data_source_id not in ids


async def test_new_row_defaults_pending_and_status_updated_at_null(
    db_session, org_a_id, org_a_deal_id
):
    repo = DataSourceRepo(db_session)
    row = await repo.create(
        {
            "org_id": org_a_id,
            "deal_id": org_a_deal_id,
            "storage_key": "org-a/key2.pdf",
            "filename": "a2.pdf",
            "declared_sha256": _DECLARED_HASH,
        }
    )
    await db_session.flush()
    await db_session.refresh(row)

    assert row.status == "pending"
    assert row.status_updated_at is None


async def test_find_dedupe_candidate_matches_declared_or_fingerprint_excludes_mismatch(
    db_session, org_a_id, org_a_deal_id
):
    repo = DataSourceRepo(db_session)

    # A still-pending row (fingerprint not yet computed) must still be found
    # via declared_sha256 -- the whole point of the OR, see the repo's
    # docstring: fingerprint alone would miss a duplicate uploaded before the
    # first upload's ingest job finishes.
    pending = await repo.create(
        {
            "org_id": org_a_id,
            "deal_id": org_a_deal_id,
            "storage_key": "org-a/pending.pdf",
            "filename": "pending.pdf",
            "declared_sha256": _DECLARED_HASH,
        }
    )
    await db_session.flush()

    candidate = await repo.find_dedupe_candidate(org_a_deal_id, _DECLARED_HASH)
    assert candidate is not None
    assert candidate.id == pending.id

    # A row whose *fingerprint* (not declared_sha256) matches is also found.
    verified = await repo.create(
        {
            "org_id": org_a_id,
            "deal_id": org_a_deal_id,
            "storage_key": "org-a/verified.pdf",
            "filename": "verified.pdf",
            "declared_sha256": "c" * 64,
        }
    )
    await db_session.flush()
    await repo.update_status(verified.id, status="verified", fingerprint=_FINGERPRINT_HASH)
    await db_session.flush()

    candidate = await repo.find_dedupe_candidate(org_a_deal_id, _FINGERPRINT_HASH)
    assert candidate is not None
    assert candidate.id == verified.id

    # A prior upload that ended up `mismatch` must NOT block a fresh
    # re-upload of the same declared hash (decided by Vansh).
    mismatched = await repo.create(
        {
            "org_id": org_a_id,
            "deal_id": org_a_deal_id,
            "storage_key": "org-a/mismatched.pdf",
            "filename": "mismatched.pdf",
            "declared_sha256": "d" * 64,
        }
    )
    await db_session.flush()
    await repo.update_status(mismatched.id, status="mismatch", fingerprint="e" * 64)
    await db_session.flush()

    assert await repo.find_dedupe_candidate(org_a_deal_id, "d" * 64) is None


async def test_dd_app_can_update_lifecycle_columns(db_session, org_a_id, org_a_deal_id):
    repo = DataSourceRepo(db_session)
    row = await repo.create(
        {
            "org_id": org_a_id,
            "deal_id": org_a_deal_id,
            "storage_key": "org-a/key3.pdf",
            "filename": "a3.pdf",
            "declared_sha256": _DECLARED_HASH,
        }
    )
    await db_session.flush()

    updated = await repo.update_status(row.id, status="verified", fingerprint=_FINGERPRINT_HASH)
    assert updated is not None
    assert updated.status == "verified"
    assert updated.fingerprint == _FINGERPRINT_HASH
    assert updated.status_updated_at is not None


async def test_dd_app_cannot_update_identity_columns(db_session, org_a_id, org_a_deal_id):
    await db_session.execute(
        text(
            "INSERT INTO data_source (org_id, deal_id, storage_key, filename, declared_sha256) "
            "VALUES (:org_id, :deal_id, :storage_key, :filename, :hash)"
        ),
        {
            "org_id": org_a_id,
            "deal_id": org_a_deal_id,
            "storage_key": "org-a/key4.pdf",
            "filename": "a4.pdf",
            "hash": _DECLARED_HASH,
        },
    )
    await db_session.flush()

    # REVOKE UPDATE, DELETE ON data_source FROM dd_app, narrowed back only for
    # (status, fingerprint, status_updated_at) -- filename is not one of them.
    with pytest.raises(DBAPIError, match="permission denied"):
        await db_session.execute(text("UPDATE data_source SET filename = 'tampered.pdf'"))


async def test_dd_app_cannot_delete_data_source(db_session, org_a_id, org_a_deal_id):
    await db_session.execute(
        text(
            "INSERT INTO data_source (org_id, deal_id, storage_key, filename, declared_sha256) "
            "VALUES (:org_id, :deal_id, :storage_key, :filename, :hash)"
        ),
        {
            "org_id": org_a_id,
            "deal_id": org_a_deal_id,
            "storage_key": "org-a/key5.pdf",
            "filename": "a5.pdf",
            "hash": _DECLARED_HASH,
        },
    )
    await db_session.flush()

    with pytest.raises(DBAPIError, match="permission denied"):
        await db_session.execute(text("DELETE FROM data_source"))


async def test_trigger_blocks_second_update_dd_app(db_session, org_a_id, org_a_deal_id):
    """The one legitimate transition (pending -> terminal) succeeds once; a
    second UPDATE to the same row -- even one only touching the granted
    columns -- is rejected by trg_data_source_one_way_status, not silently
    accepted."""
    repo = DataSourceRepo(db_session)
    row = await repo.create(
        {
            "org_id": org_a_id,
            "deal_id": org_a_deal_id,
            "storage_key": "org-a/key6.pdf",
            "filename": "a6.pdf",
            "declared_sha256": _DECLARED_HASH,
        }
    )
    await db_session.flush()

    first = await repo.update_status(row.id, status="verified", fingerprint=_FINGERPRINT_HASH)
    assert first is not None

    with pytest.raises(DBAPIError, match="status is final once left pending"):
        await repo.update_status(row.id, status="mismatch", fingerprint=_FINGERPRINT_HASH)


async def test_trigger_allows_verified_to_ocr_needed(db_session, org_a_id, org_a_deal_id):
    """docs/plans/start-analysis-flow-alpha.md's Option A: the parser's
    no_extractable_text signal (SIM-350) must be able to land as a
    verified -> ocr_needed transition -- the one deliberate carve-out this
    migration added to the otherwise one-way trigger."""
    repo = DataSourceRepo(db_session)
    row = await repo.create(
        {
            "org_id": org_a_id,
            "deal_id": org_a_deal_id,
            "storage_key": "org-a/key8.pdf",
            "filename": "a8.pdf",
            "declared_sha256": _DECLARED_HASH,
        }
    )
    await db_session.flush()
    await repo.update_status(row.id, status="verified", fingerprint=_FINGERPRINT_HASH)
    await db_session.flush()

    updated = await repo.update_status(row.id, status="ocr_needed", fingerprint=_FINGERPRINT_HASH)
    assert updated is not None
    assert updated.status == "ocr_needed"
    # Implementer trap the plan calls out: fingerprint must still be the
    # row's real (already-verified) hash, never wiped to None.
    assert updated.fingerprint == _FINGERPRINT_HASH


async def test_trigger_still_blocks_ocr_needed_as_a_dead_end(db_session, org_a_id, org_a_deal_id):
    """ocr_needed stays terminal -- the carve-out is narrowly
    verified->ocr_needed only, not a reopening of the lifecycle."""
    repo = DataSourceRepo(db_session)
    row = await repo.create(
        {
            "org_id": org_a_id,
            "deal_id": org_a_deal_id,
            "storage_key": "org-a/key9.pdf",
            "filename": "a9.pdf",
            "declared_sha256": _DECLARED_HASH,
        }
    )
    await db_session.flush()
    await repo.update_status(row.id, status="verified", fingerprint=_FINGERPRINT_HASH)
    await db_session.flush()
    await repo.update_status(row.id, status="ocr_needed", fingerprint=_FINGERPRINT_HASH)
    await db_session.flush()

    with pytest.raises(DBAPIError, match="status is final once left pending"):
        await repo.update_status(row.id, status="verified", fingerprint=_FINGERPRINT_HASH)


async def test_trigger_still_blocks_verified_to_mismatch(db_session, org_a_id, org_a_deal_id):
    """The carve-out is specifically verified->ocr_needed -- every other
    post-verified transition is still rejected."""
    repo = DataSourceRepo(db_session)
    row = await repo.create(
        {
            "org_id": org_a_id,
            "deal_id": org_a_deal_id,
            "storage_key": "org-a/key10.pdf",
            "filename": "a10.pdf",
            "declared_sha256": _DECLARED_HASH,
        }
    )
    await db_session.flush()
    await repo.update_status(row.id, status="verified", fingerprint=_FINGERPRINT_HASH)
    await db_session.flush()

    with pytest.raises(DBAPIError, match="status is final once left pending"):
        await repo.update_status(row.id, status="mismatch", fingerprint=_FINGERPRINT_HASH)


def test_trigger_blocks_second_update_even_via_table_owner(owner_conn, org_a_id, org_a_deal_id):
    """Confirms the trigger fires for EVERY role, not just dd_app -- the part
    of the design that closes the gap plain GRANT/REVOKE alone would leave
    open (the owning role bypasses those). Run directly on owner_conn
    (doadmin), with no dd_app/RLS involved at all.
    """
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO data_source (org_id, deal_id, storage_key, filename, declared_sha256) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (org_a_id, org_a_deal_id, "org-a/key7.pdf", "a7.pdf", _DECLARED_HASH),
        )
        data_source_id = cur.fetchone()[0]

        cur.execute(
            "UPDATE data_source SET status = 'verified', fingerprint = %s, "
            "status_updated_at = now() WHERE id = %s",
            (_FINGERPRINT_HASH, data_source_id),
        )

        with pytest.raises(Exception, match="status is final once left pending"):
            cur.execute(
                "UPDATE data_source SET status = 'mismatch' WHERE id = %s", (data_source_id,)
            )
