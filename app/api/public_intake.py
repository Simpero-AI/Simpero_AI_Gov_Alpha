from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.intake_security import encode_intake_session_jwt
from app.core.public_dependencies import get_public_link_db
from app.models.deal_intake_link import DealIntakeLink
from app.repo.HumanAuditRepo import HumanAuditRepo
from app.repo.IntakeLinkRepo import IntakeLinkRepo
from app.schemas.public_intake import IntakeEmailVerifyRequest, IntakeSessionResponse

router = APIRouter(prefix="/public/intake", tags=["public-intake"])


@router.post("/{token}/session", response_model=IntakeSessionResponse)
async def create_intake_session(
    body: IntakeEmailVerifyRequest,
    session_and_link: tuple[AsyncSession, DealIntakeLink] = Depends(get_public_link_db),
) -> IntakeSessionResponse | JSONResponse:
    session, link = session_and_link
    tried_email = body.email

    if tried_email.lower() != link.recipient_email.lower():
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
