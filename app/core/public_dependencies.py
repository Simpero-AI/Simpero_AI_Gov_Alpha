from collections.abc import AsyncGenerator

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.intake_security import decode_intake_session_jwt, sha256_hex
from app.core.public_database import PublicAsyncSessionLocal
from app.models.deal_intake_link import DealIntakeLink
from app.repo.IntakeLinkRepo import IntakeLinkRepo


async def get_public_link_db(
    token: str,
) -> AsyncGenerator[tuple[AsyncSession, DealIntakeLink], None]:
    """Used by exactly one route (P3): POST /api/public/intake/{token}/session.

    PublicAsyncSessionLocal is a SEPARATE engine bound to dd_public -- never
    the dd_app pool app/core/dependencies.py uses. Tenant scope is derived
    from the database, never asserted by the caller: the only input here is
    a secret (the raw token) that must match a live, pending, unexpired row
    via the intake_token_lookup keyhole policy (P1-03).
    """
    async with PublicAsyncSessionLocal() as session, session.begin():
        # Phase 1. FIRST statement in the transaction. The only thing
        # visible at this point is one link row, via keyhole policy A
        # (intake_token_lookup) -- everything else is default-deny.
        token_hash = sha256_hex(token)
        await session.execute(
            text("SELECT set_config('app.intake_token_hash', :h, true)"),
            {"h": token_hash},
        )
        link = await IntakeLinkRepo(session).get_by_token_hash(token_hash)
        if link is None:
            # Never 403 -- see brief section 5.2. A malformed/unknown/expired/
            # revoked/submitted token all produce the exact same 404, same
            # body, so this endpoint can never be used as an oracle.
            raise HTTPException(status_code=404, detail="Not found")

        # Phase 2. org_id comes off the link row itself -- clerk_org_id is a
        # denormalized column on deal_intake_link (P1-01), NOT a join through
        # `organisation`. organisation is RLS'd on the same clerk_org_id, so
        # reading link.organisation.clerk_org_id here would fail: app.org_id
        # isn't set yet, and organisation's own policy would return nothing.
        # Both GUCs set in the SAME statement, before any tenant-table query
        # runs after this point.
        await session.execute(
            text(
                "SELECT set_config('app.org_id', :tid, true), "
                "set_config('app.intake_deal_id', :did, true)"
            ),
            {"tid": link.clerk_org_id, "did": str(link.deal_id)},
        )
        yield session, link


async def get_public_session_db(
    session_token: str,
) -> AsyncGenerator[tuple[AsyncSession, DealIntakeLink], None]:
    """Used by every route AFTER /session (all P3): /questions, /answers,
    /uploads/*, /submit. The raw link token is never sent again past this
    point -- only this short-lived, app-signed session token.

    decode_intake_session_jwt raising AuthenticationError on a bad token is
    NOT caught/converted to HTTPException here -- that's a P3 route-handler
    concern (wrapping this dependency), not this dependency's.
    """
    claims = decode_intake_session_jwt(session_token)
    async with PublicAsyncSessionLocal() as session, session.begin():
        # Phase 1. FIRST statement. Keyhole policy B (intake_session_lookup),
        # keyed on link_id from the verified session claim.
        await session.execute(
            text("SELECT set_config('app.intake_link_id', :lid, true)"),
            {"lid": str(claims.link_id)},
        )
        link = await IntakeLinkRepo(session).get_by_id(claims.link_id)
        if link is None:
            raise HTTPException(status_code=404, detail="Not found")

        # Phase 2. Same clerk_org_id column, same reasoning as get_public_link_db.
        await session.execute(
            text(
                "SELECT set_config('app.org_id', :tid, true), "
                "set_config('app.intake_deal_id', :did, true)"
            ),
            {"tid": link.clerk_org_id, "did": str(link.deal_id)},
        )
        yield session, link
