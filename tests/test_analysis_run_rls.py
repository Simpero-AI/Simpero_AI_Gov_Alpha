"""RLS/grant/constraint tests for analysis_run
(docs/plans/start-analysis-flow-alpha.md, D5/D6). Mirrors
tests/test_data_source_rls.py's fixtures and idioms.
"""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.repo.AnalysisRunRepo import AnalysisRunRepo


def _insert_deal(cur, org_pk: int, name: str = "Test Deal") -> str:
    cur.execute("INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id", (org_pk, name))
    return str(cur.fetchone()[0])


@pytest.fixture
def org_a_id(owner_conn, test_org_id) -> int:
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
    with owner_conn.cursor() as cur:
        return _insert_deal(cur, org_a_id, "Org A's deal")


@pytest.fixture
def org_b_analysis_run_id(owner_conn) -> Iterator[str]:
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
            "INSERT INTO analysis_run (org_id, deal_id) VALUES (%s, %s) RETURNING id",
            (org_b_pk, deal_id),
        )
        run_id = cur.fetchone()[0]

    yield str(run_id)

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM analysis_run WHERE id = %s", (run_id,))
        cur.execute("DELETE FROM deals WHERE id = %s", (deal_id,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_b_pk,))


async def test_org_isolation_hides_other_org_run(db_session, org_a_id, org_b_analysis_run_id):
    result = await db_session.execute(
        text("SELECT id FROM analysis_run WHERE id = :id"), {"id": org_b_analysis_run_id}
    )
    assert result.first() is None


async def test_org_isolation_still_shows_own_org_run(
    db_session, org_a_id, org_a_deal_id, org_b_analysis_run_id
):
    repo = AnalysisRunRepo(db_session)
    own = await repo.create({"org_id": org_a_id, "deal_id": org_a_deal_id})
    await db_session.flush()

    fetched = await repo.get_by_id(own.id)
    assert fetched is not None
    assert str(fetched.deal_id) == org_a_deal_id

    all_rows = await db_session.execute(text("SELECT id FROM analysis_run"))
    ids = [str(r[0]) for r in all_rows.fetchall()]
    assert str(own.id) in ids
    assert org_b_analysis_run_id not in ids


async def test_new_run_defaults_queued_with_no_parse_jobs(db_session, org_a_id, org_a_deal_id):
    repo = AnalysisRunRepo(db_session)
    row = await repo.create({"org_id": org_a_id, "deal_id": org_a_deal_id})
    await db_session.flush()
    await db_session.refresh(row)

    assert row.status == "queued"
    assert row.job_name == "parsing"  # server_default; "extraction" is unused (point 4)
    assert row.parse_jobs is None
    assert row.error_message is None
    assert row.started_at is not None
    assert row.ended_at is None
    assert row.job_comments is None


async def test_update_progress_stamps_ended_at_only_on_terminal_status(
    db_session, org_a_id, org_a_deal_id
):
    """ended_at is never a caller-supplied value -- it's stamped
    automatically, server-side, the one time status flips to a terminal
    value. A non-terminal progress update (queued -> in_progress) must leave
    it NULL."""
    repo = AnalysisRunRepo(db_session)
    row = await repo.create({"org_id": org_a_id, "deal_id": org_a_deal_id})
    await db_session.flush()

    in_progress = await repo.update_progress(row.id, status="in_progress")
    await db_session.flush()
    await db_session.refresh(in_progress)
    assert in_progress.ended_at is None

    successful = await repo.update_progress(row.id, status="successful")
    await db_session.flush()
    await db_session.refresh(successful)
    assert successful.ended_at is not None
    assert successful.ended_at >= successful.started_at


async def test_dd_app_can_update_mutable_columns(db_session, org_a_id, org_a_deal_id):
    repo = AnalysisRunRepo(db_session)
    row = await repo.create({"org_id": org_a_id, "deal_id": org_a_deal_id})
    await db_session.flush()

    updated = await repo.update_progress(
        row.id, status="in_progress", parse_jobs=[{"data_source_id": "x"}]
    )
    assert updated.status == "in_progress"
    assert updated.parse_jobs == [{"data_source_id": "x"}]
    assert updated.job_comments is None  # not written until a terminal status

    finished = await repo.update_progress(
        row.id,
        status="successful",
        job_comments=[
            {"dataSourceId": "x", "fileName": "a.pdf", "status": "parsed", "comment": "ok"}
        ],
    )
    assert finished.job_comments == [
        {"dataSourceId": "x", "fileName": "a.pdf", "status": "parsed", "comment": "ok"}
    ]


async def test_dd_app_cannot_update_identity_columns(db_session, org_a_id, org_a_deal_id):
    await db_session.execute(
        text("INSERT INTO analysis_run (org_id, deal_id) VALUES (:org_id, :deal_id)"),
        {"org_id": org_a_id, "deal_id": org_a_deal_id},
    )
    await db_session.flush()

    # REVOKE UPDATE, DELETE ON analysis_run FROM dd_app, narrowed back only
    # for (status, parse_jobs, error_message, job_comments, ended_at, updated_at) --
    # deal_id is not one of them.
    with pytest.raises(DBAPIError, match="permission denied"):
        await db_session.execute(text("UPDATE analysis_run SET deal_id = gen_random_uuid()"))


async def test_dd_app_cannot_update_job_name(db_session, org_a_id, org_a_deal_id):
    """job_name is identity, append-only, same as deal_id -- separate test
    (not appended to the identity-columns test above) because a permission
    error aborts the rest of that test's transaction."""
    await db_session.execute(
        text("INSERT INTO analysis_run (org_id, deal_id) VALUES (:org_id, :deal_id)"),
        {"org_id": org_a_id, "deal_id": org_a_deal_id},
    )
    await db_session.flush()

    with pytest.raises(DBAPIError, match="permission denied"):
        await db_session.execute(text("UPDATE analysis_run SET job_name = 'extraction'"))


async def test_dd_app_cannot_delete_analysis_run(db_session, org_a_id, org_a_deal_id):
    await db_session.execute(
        text("INSERT INTO analysis_run (org_id, deal_id) VALUES (:org_id, :deal_id)"),
        {"org_id": org_a_id, "deal_id": org_a_deal_id},
    )
    await db_session.flush()

    with pytest.raises(DBAPIError, match="permission denied"):
        await db_session.execute(text("DELETE FROM analysis_run"))


async def test_active_for_deal_finds_queued_and_in_progress_not_terminal(
    db_session, org_a_id, org_a_deal_id
):
    repo = AnalysisRunRepo(db_session)
    run = await repo.create({"org_id": org_a_id, "deal_id": org_a_deal_id})
    await db_session.flush()

    assert (await repo.active_for_deal(org_a_deal_id)) is not None

    await repo.update_progress(run.id, status="successful")
    await db_session.flush()
    assert (await repo.active_for_deal(org_a_deal_id)) is None


async def test_uq_analysis_run_active_blocks_second_active_run_same_deal(
    db_session, org_a_id, org_a_deal_id
):
    """D6: the partial unique index is the actual double-submit guarantee --
    a second queued/in_progress run for the same deal must fail at the DB
    level, not just be caught by a prior SELECT."""
    repo = AnalysisRunRepo(db_session)
    await repo.create({"org_id": org_a_id, "deal_id": org_a_deal_id})
    await db_session.flush()

    await repo.create({"org_id": org_a_id, "deal_id": org_a_deal_id})
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_uq_analysis_run_active_allows_new_run_once_prior_is_terminal(
    db_session, org_a_id, org_a_deal_id
):
    repo = AnalysisRunRepo(db_session)
    first = await repo.create({"org_id": org_a_id, "deal_id": org_a_deal_id})
    await db_session.flush()
    await repo.update_progress(first.id, status="failed", error_message="boom")
    await db_session.flush()

    second = await repo.create({"org_id": org_a_id, "deal_id": org_a_deal_id})
    await db_session.flush()  # must not raise
    assert second.id != first.id
