import os
import secrets
from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime, timedelta
from typing import cast

import psycopg2
import pytest
from saq.queue.redis import RedisQueue
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.intake_security import sha256_hex
from app.core.public_database import PublicAsyncSessionLocal
from app.jobs.queue import get_queue

TEST_org_id = "test-tenant-00000000"
TEST_user_id = "test-user-00000000"
_INTAKE_LINK_EXPIRES_AT = datetime.now(UTC) + timedelta(days=7)


@pytest.fixture(scope="session")
def test_org_id() -> str:
    return TEST_org_id


@pytest.fixture
def owner_conn() -> Iterator["psycopg2.extensions.connection"]:
    """Direct doadmin (table-owner) connection for fixture setup/teardown that
    must bypass RLS — e.g. seeding a second tenant's row that a dd_app session
    (scoped to one org via SET LOCAL) could never create for another org.
    Never used by app code, tests only.
    """
    dsn = os.environ["ALEMBIC_DATABASE_URL"].replace("+psycopg2", "").replace("+asyncpg", "")
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def org_a_id(owner_conn, test_org_id) -> int:
    """The organisation backing the test session's own app.org_id. Moved
    here from tests/test_deals_rls.py (its original home) since more than
    one test module now needs it -- e.g. tests/test_workspace_config.py."""
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (clerk_org_id) DO NOTHING",
            (test_org_id, "Org A"),
        )
        cur.execute("SELECT id FROM organisation WHERE clerk_org_id = %s", (test_org_id,))
        return cur.fetchone()[0]


@pytest.fixture
def user_a_id(owner_conn, org_a_id, test_org_id) -> int:
    """A user inside org A. Needed because `mandates.user_id` is NOT NULL --
    any test seeding a mandate (tests/test_workspace_config.py,
    tests/test_screening_evaluators.py) needs a real users row to point at.
    Same ON CONFLICT DO NOTHING / SELECT back idiom as org_a_id above, so it
    survives a previous run that left the row behind."""
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (org_id, role, login_method, clerk_user_id, clerk_org_id, status) "
            "VALUES (%s, 'admin', 'email', %s, %s, 'active') "
            "ON CONFLICT (clerk_user_id) DO NOTHING",
            (org_a_id, TEST_user_id, test_org_id),
        )
        cur.execute("SELECT id FROM users WHERE clerk_user_id = %s", (TEST_user_id,))
        return cur.fetchone()[0]


def _insert_deal(cur, org_pk: int, name: str = "Test Deal") -> str:
    cur.execute("INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id", (org_pk, name))
    return str(cur.fetchone()[0])


@pytest.fixture
def org_a_deal_id(owner_conn, org_a_id) -> str:
    """Moved here from tests/test_public_dependencies.py (its original home,
    same reasoning as org_a_id above) since tests/test_public_intake_session.py
    now needs it too. No teardown, deliberately -- same reasoning as
    test_intake_link_rls.py's org_a_deal_id."""
    with owner_conn.cursor() as cur:
        return _insert_deal(cur, org_a_id, "Org A's deal")


@pytest.fixture
def pending_link_with_token(
    owner_conn, org_a_id, org_a_deal_id, user_a_id, test_org_id
) -> Iterator[dict]:
    """Moved here from tests/test_public_dependencies.py -- same reasoning as
    org_a_deal_id above. A pending, unexpired deal_intake_link row seeded via
    owner_conn (bypasses RLS) -- we control the raw token here (never
    stored), and seed only its SHA-256 into token_hash, mirroring how the
    real create-link route (P3) would produce it."""
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
                _INTAKE_LINK_EXPIRES_AT,
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


@pytest.fixture
async def db_session(test_org_id: str) -> AsyncGenerator[AsyncSession, None]:
    """
    Test DB session fixture that replicates the production get_db pattern exactly.

    Issues SET LOCAL app.org_id inside a transaction, then rolls back after each
    test to keep tests isolated — no test data persists between tests.

    NOTE: This fixture requires a real PostgreSQL instance (not SQLite). RLS policies
    and current_setting() are PostgreSQL-specific. Tests that bypass this fixture and
    create sessions without SET LOCAL will silently bypass RLS, returning unfiltered
    data and masking real isolation bugs.

    TODO: Set TEST_DATABASE_URL in .env (or CI environment) and wire AsyncSessionLocal
    to it for test runs. Consider pytest-docker or a dedicated test schema.
    """
    async with AsyncSessionLocal() as session, session.begin():
        # set_config(..., true) IS "SET LOCAL" in function form — a bare
        # `SET LOCAL x = :p` cannot bind parameters (Postgres's SET grammar
        # doesn't accept them), which is exactly why app/core/dependencies.py's
        # get_db uses this same form. Kept in sync with that dependency.
        await session.execute(
            text("SELECT set_config('app.org_id', :tid, true)"),
            {"tid": test_org_id},
        )
        yield session
        await session.rollback()


@pytest.fixture
async def public_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Raw dd_public session, NO GUC set by default -- callers set whichever
    GUC(s) (app.intake_token_hash / app.intake_link_id / app.org_id /
    app.intake_deal_id) their own test needs, inline, via set_config. Used by
    P1-03/04/06/08/09/05's test suites."""
    async with PublicAsyncSessionLocal() as session, session.begin():
        yield session
        await session.rollback()


@pytest.fixture
async def clear_rate_limit_keys() -> AsyncGenerator[None, None]:
    """Deletes every `ratelimit:*` key in Valkey, both before and after the
    test. Non-autouse (explicit opt-in) -- only test modules that actually
    exercise app.core.rate_limit_middleware need Valkey reachable; everything
    else is unaffected. Without the before-clear, keys left behind by a
    previous (possibly failed) run could pre-trip a fresh run's limit; without
    the after-clear, this test's own requests would count against whatever
    runs next.
    """
    # get_queue() is statically typed as the abstract saq.Queue; VALKEY_URL
    # is always redis://, so it's always a RedisQueue with a `.redis` client.
    redis = cast(RedisQueue, get_queue()).redis

    async def _clear() -> None:
        async for key in redis.scan_iter("ratelimit:*"):
            await redis.delete(key)

    await _clear()
    yield
    await _clear()
    # get_queue() is a process-wide (lru_cache) singleton, but pytest-asyncio
    # (asyncio_mode=auto) gives each test function its own event loop by
    # default. redis-asyncio's connections are bound to the loop that opened
    # them, so leaving this one open would crash the NEXT test that reuses
    # get_queue().redis with "Future attached to a different loop". Closing
    # here is safe -- redis-py reconnects lazily on the next command, in
    # whatever loop is running then.
    await redis.aclose()
