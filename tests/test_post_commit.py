"""Unit tests for the post-commit hook primitive (no database).

The end-to-end "enqueue runs only after the row commits" behaviour is covered by
test_uploads_api.test_complete_enqueues_only_after_the_row_is_committed (needs
Postgres). These tests pin the primitive itself: order, clear-after-run, no-op.
"""

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.post_commit import add_post_commit_hook, run_post_commit_hooks


class _FakeSession:
    """Duck-types the two attributes the primitive touches: .info (a dict that
    outlives the real session's close)."""

    def __init__(self) -> None:
        self.info: dict = {}


async def test_hooks_run_in_registration_order() -> None:
    session = cast(AsyncSession, _FakeSession())
    calls: list[str] = []

    async def _record(name: str) -> None:
        calls.append(name)

    add_post_commit_hook(session, lambda: _record("a"))
    add_post_commit_hook(session, lambda: _record("b"))
    await run_post_commit_hooks(session)

    assert calls == ["a", "b"]


async def test_hooks_are_cleared_after_running() -> None:
    session = cast(AsyncSession, _FakeSession())
    calls: list[str] = []

    async def _record() -> None:
        calls.append("x")

    add_post_commit_hook(session, _record)
    await run_post_commit_hooks(session)
    # A second drain fires nothing -- hooks are popped, so a reused session object
    # cannot double-enqueue.
    await run_post_commit_hooks(session)

    assert calls == ["x"]


async def test_no_hooks_registered_is_a_noop() -> None:
    session = cast(AsyncSession, _FakeSession())
    await run_post_commit_hooks(session)  # must not raise
