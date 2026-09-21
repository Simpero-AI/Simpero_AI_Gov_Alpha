"""Run a side effect only AFTER a request's transaction has committed.

Some side effects must not happen unless the data they depend on is durable.
The upload handlers enqueue an ingest job that immediately looks the just-written
data_source row up under RLS: enqueue it inside the request transaction and a
fast worker can dequeue before the commit (transient "data_source not found"),
or -- if the commit later fails -- the job is orphaned against a row that never
persisted. A FastAPI BackgroundTask does not fix this: background tasks run
BEFORE the get_db yield-dependency's commit, so the enqueue still fires before
the row is durable and still fires even when the commit ultimately fails.

The session-owning dependency (get_db / get_public_session_db) is the only place
that knows the transaction actually committed. A handler registers a hook here;
the dependency awaits the hooks AFTER a successful commit and never on a
rollback. The hook is a zero-argument coroutine factory -- it must NOT touch the
session (which is closed by then); it captures the values it needs at
registration time.
"""

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

_HOOKS_KEY = "post_commit_hooks"

PostCommitHook = Callable[[], Awaitable[None]]


def add_post_commit_hook(session: AsyncSession, hook: PostCommitHook) -> None:
    """Register `hook` to run after this session's transaction commits cleanly.

    Stored on session.info (a plain dict that survives the session's close), so it
    is readable by the dependency's after-commit code even though the session is
    closed by the time the hooks run. Order preserved; rolled-back transactions
    never invoke their hooks.
    """
    session.info.setdefault(_HOOKS_KEY, []).append(hook)


async def run_post_commit_hooks(session: AsyncSession) -> None:
    """Invoke and clear the session's post-commit hooks, in registration order.

    Called by the session-owning dependency ONLY after the transaction has
    committed successfully. Popped so a reused session object cannot double-fire
    them. A hook that raises propagates -- the commit already happened, so a
    failure here is a real error to surface, not one to swallow.
    """
    hooks: list[PostCommitHook] = session.info.pop(_HOOKS_KEY, [])
    for hook in hooks:
        await hook()
