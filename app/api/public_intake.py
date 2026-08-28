import hmac

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.intake_security import encode_intake_session_jwt
from app.core.public_dependencies import get_public_link_db, get_public_session_db
from app.models.deal_intake_link import DealIntakeLink
from app.models.organisation import Organisation
from app.repo.HumanAuditRepo import HumanAuditRepo
from app.repo.IntakeLinkRepo import IntakeLinkRepo
from app.schemas.public_intake import (
    IntakeEmailVerifyRequest,
    IntakeQuestionResponse,
    IntakeQuestionsResponse,
    IntakeSessionResponse,
)

router = APIRouter(prefix="/public/intake", tags=["public-intake"])

_LOCKOUT_THRESHOLD = 5


async def _org_name_for_link(db: AsyncSession, link: DealIntakeLink) -> str:
    """Only `name` -- link.org_id (already readable, full-table SELECT grant
    on deal_intake_link) covers the FK value; dd_public's grant on
    organisation is column-restricted to (id, name, clerk_org_id), so this
    stays a scoped select rather than select(Organisation)."""
    name = await db.scalar(
        select(Organisation.name).where(Organisation.clerk_org_id == link.clerk_org_id)
    )
    assert name is not None  # get_public_session_db already vouched for this clerk_org_id
    return name


@router.post("/{token}/session", response_model=IntakeSessionResponse)
async def create_intake_session(
    body: IntakeEmailVerifyRequest,
    session_and_link: tuple[AsyncSession, DealIntakeLink] = Depends(get_public_link_db),
) -> IntakeSessionResponse | JSONResponse:
    session, link = session_and_link

    # Unconditional lockout: once failed_attempts hits the threshold, every
    # further attempt 404s -- even one with the correct email -- and does so
    # WITHOUT bumping failed_attempts or writing an audit row. The 5th real
    # mismatch already produced the audit trail; writing on every subsequent
    # hammering attempt would flood the audit log P3-13 later reviews.
    if link.failed_attempts >= _LOCKOUT_THRESHOLD:
        return JSONResponse(status_code=404, content={"detail": "Not found"})

    tried_email = body.email

    if not hmac.compare_digest(tried_email.lower(), link.recipient_email.lower()):
        await IntakeLinkRepo(session).bump_failed_attempt(link.id)
        await HumanAuditRepo(session).append(
            {
                "org_id": link.org_id,
                "actor_id": None,
                "actor_email": tried_email,
                "event_type": "intake_email_attempt_failed",
                "deal_id": link.deal_id,
                "payload": {"link_id": str(link.id)},
            }
        )
        # Return the Response directly (never raise) -- see this file's
        # header note in the plan: raising here would propagate back into
        # get_public_link_db's generator at its `yield`, rolling back both
        # writes above via session.begin()'s exception-exit path. Returning
        # a Response subclass exits the generator cleanly and commits.
        return JSONResponse(status_code=404, content={"detail": "Not found"})

    token = encode_intake_session_jwt(link.id, tried_email)
    await HumanAuditRepo(session).append(
        {
            "org_id": link.org_id,
            "actor_id": None,
            "actor_email": tried_email,
            "event_type": "intake_email_attempt_succeeded",
            "deal_id": link.deal_id,
            "payload": {"link_id": str(link.id)},
        }
    )
    return IntakeSessionResponse(session_token=token)


@router.get("/questions", response_model=IntakeQuestionsResponse)
async def get_intake_questions(
    session_and_link: tuple[AsyncSession, DealIntakeLink] = Depends(get_public_session_db),
) -> IntakeQuestionsResponse:
    db, link = session_and_link
    org_name = await _org_name_for_link(db, link)

    # questions_snapshot is nullable on the model but every link is created
    # with one (app/api/deals.py) -- the type allows None only for cases the
    # real data model never produces, so an empty list here, not a 500.
    raw_questions = (link.questions_snapshot or {}).get("questions", [])
    ordered = sorted(raw_questions, key=lambda q: q["display_order"])

    return IntakeQuestionsResponse(
        org_name=org_name,
        questions=[IntakeQuestionResponse(**q) for q in ordered],
    )
