from collections.abc import AsyncGenerator
from typing import Any

import httpx
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.exceptions import AuthenticationError
from app.core.post_commit import run_post_commit_hooks
from app.core.security import decode_clerk_jwt, fetch_clerk_organization
from app.models.organisation import Organisation, OrgType, Users
from app.repo.UserRepo import UserRepo


async def get_claims(authorization: str = Header(...)) -> dict[str, Any]:
    """Verify the Clerk bearer token and return its claims.

    FastAPI caches dependency results per-request, so routes that need both
    the session (get_db) and the caller's identity resolve this only once.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header",
        )
    token = authorization.removeprefix("Bearer ").strip()
    try:
        return await decode_clerk_jwt(token)
    except AuthenticationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        # JWKS unreachable is our problem, not the caller's.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to verify credentials (signing keys unavailable)",
        ) from exc


async def _ensure_user_provisioned(session: AsyncSession, claims: dict[str, Any]) -> None:
    """JIT provisioning: first authenticated request creates the Users row
    (and the Organisation row, fetched from Clerk's Backend API, if the org
    is brand new). RLS already scopes both lookups to app.org_id.

    name/email stay NULL here — the token doesn't carry them; the frontend
    pushes them via POST /auth/sync-profile after login.
    """
    existing = await session.scalar(select(Users).where(Users.clerk_user_id == claims["user_id"]))
    if existing is not None:
        # A previously-removed-then-re-invited member's row stays
        # status='inactive' forever otherwise — their Clerk org membership
        # is genuinely active again post-re-invite, but nothing else tells
        # the DB row. role/name/email JIT-provisioning behavior below is
        # unrelated and untouched by this reactivation.
        if existing.status != "active":
            await UserRepo(session).reactivate(claims["user_id"])
        return

    # ON CONFLICT DO NOTHING throughout: on first login the frontend fires
    # /auth/me and /auth/sync-profile near-simultaneously, so two transactions
    # can race to provision the same user/org.
    org_pk = await session.scalar(
        select(Organisation.id).where(Organisation.clerk_org_id == claims["tenant_id"])
    )
    if org_pk is None:
        clerk_org = await fetch_clerk_organization(claims["tenant_id"])
        raw_type = (clerk_org.get("public_metadata") or {}).get("type")
        try:
            org_type = OrgType(raw_type) if raw_type else None
        except ValueError:
            org_type = None
        await session.execute(
            pg_insert(Organisation)
            .values(
                clerk_org_id=claims["tenant_id"],
                name=clerk_org.get("name") or claims["tenant_id"],
                type=org_type,
            )
            .on_conflict_do_nothing(index_elements=["clerk_org_id"])
        )
        org_pk = await session.scalar(
            select(Organisation.id).where(Organisation.clerk_org_id == claims["tenant_id"])
        )
        if org_pk is None:  # pragma: no cover — RLS misconfig would surface here
            raise RuntimeError("Organisation row not visible after provisioning")
    user_repo = UserRepo(session)
    await user_repo.upsert(
        {
            "org_id": org_pk,
            "clerk_user_id": claims["user_id"],
            "clerk_org_id": claims["tenant_id"],
            "role": claims.get("org_role") or "member",
            "login_method": "clerk",
        }
    )


async def get_db(
    claims: dict[str, Any] = Depends(get_claims),
) -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that:
      1. Verifies the Clerk JWT (via get_claims) and extracts the org id.
      2. Opens an async DB session.
      3. Issues SET LOCAL app.org_id as the FIRST statement inside the transaction.
      4. JIT-provisions the Users (and Organisation) row on first login.
      5. Yields the session to the route handler.
      6. Commits on success, rolls back on error, always closes.

    WHY SET LOCAL (not SET or SET SESSION):
      PgBouncer in transaction-pooling mode assigns a Postgres backend connection to a client
      only for the duration of one transaction. After commit/rollback, the backend connection
      is reclaimed and may be handed to a different tenant's next request.

      SET SESSION (connection-scoped) would persist on the connection after the transaction
      ends — leaking tenant_id to the next tenant that gets that connection from PgBouncer.

      SET LOCAL is transaction-scoped: it is automatically reset when the transaction ends,
      making it safe regardless of which backend connection PgBouncer assigns.

    WHY THIS MUST BE THE FIRST STATEMENT IN THE TRANSACTION:
      PostgreSQL evaluates RLS policies at statement execution time using current_setting().
      If any query runs before SET LOCAL, it sees NULL or a stale tenant_id and either
      returns no rows or returns the wrong rows.
    """
    async with AsyncSessionLocal() as session, session.begin():
        # SET LOCAL must be the first SQL in this transaction — see docstring above.
        # set_config(..., true) IS "SET LOCAL" in function form; a bare
        # `SET LOCAL x = :p` cannot bind parameters (Postgres rejects $1 there).
        await session.execute(
            text("SELECT set_config('app.org_id', :tid, true)"),
            {"tid": claims["tenant_id"]},
        )
        await _ensure_user_provisioned(session, claims)
        yield session
    # Reached only when the `session.begin()` block exited cleanly -- i.e. the
    # transaction COMMITTED. A handler error or a failed commit propagates out of
    # the block above and skips this, so post-commit hooks run if and only if the
    # data is durable. This is why an ingest enqueue registered via
    # add_post_commit_hook cannot dequeue before its row exists, nor be orphaned by
    # a rollback -- unlike a FastAPI BackgroundTask, which runs before this point.
    await run_post_commit_hooks(session)
