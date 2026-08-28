import hmac

from fastapi import APIRouter, Depends, HTTPException, status
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
    AnswerInput,
    DraftAnswerResponse,
    IntakeEmailVerifyRequest,
    IntakeQuestionResponse,
    IntakeQuestionsResponse,
    IntakeSessionResponse,
    SubmitAnswersRequest,
    SubmitAnswersResponse,
)

router = APIRouter(prefix="/public/intake", tags=["public-intake"])

_LOCKOUT_THRESHOLD = 5
_MAX_ANSWER_LENGTH = 4000


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


def _validate_answers(answers: list[AnswerInput], lookup: dict[str, dict]) -> None:
    seen: set[str] = set()
    for entry in answers:
        if entry.question_key in seen:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Duplicate question_key: {entry.question_key!r}",
            )
        seen.add(entry.question_key)

        question = lookup.get(entry.question_key)
        if question is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unknown question_key: {entry.question_key!r}",
            )
        if len(entry.answer) > _MAX_ANSWER_LENGTH:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Answer for {entry.question_key!r} exceeds {_MAX_ANSWER_LENGTH} characters",
            )
        if question["required"] and not entry.answer.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Required question {entry.question_key!r} cannot be blank",
            )


def _seed_draft(snapshot_questions: list[dict]) -> dict[str, dict]:
    """Every question at answered=False, answer="" -- the first-call seed of
    the read-merge-write draft, keyed by question_key so overlay is a plain
    dict update."""
    return {
        q["question_key"]: {
            "question_key": q["question_key"],
            "prompt": q["prompt"],
            "answer": "",
            "answered": False,
        }
        for q in snapshot_questions
    }


@router.post("/answers", response_model=SubmitAnswersResponse)
async def submit_intake_answers(
    body: SubmitAnswersRequest,
    session_and_link: tuple[AsyncSession, DealIntakeLink] = Depends(get_public_session_db),
) -> SubmitAnswersResponse:
    db, link = session_and_link
    snapshot_questions = (link.questions_snapshot or {}).get("questions", [])
    lookup = {q["question_key"]: q for q in snapshot_questions}

    _validate_answers(body.answers, lookup)

    draft = (
        {a["question_key"]: a for a in link.draft_answers["answers"]}
        if link.draft_answers is not None
        else _seed_draft(snapshot_questions)
    )
    for entry in body.answers:
        draft[entry.question_key] = {
            "question_key": entry.question_key,
            "prompt": lookup[entry.question_key]["prompt"],
            "answer": entry.answer,
            "answered": bool(entry.answer.strip()),
        }

    merged = {"schema_version": 1, "answers": list(draft.values())}
    updated = await IntakeLinkRepo(db).update_draft_answers(link.id, merged)
    if not updated:
        # Same 404-only contract as every other public route -- a stale call
        # arriving after the link left `pending` (submitted/revoked/expired)
        # matches zero rows under dd_public's intake_link_status_update RLS
        # policy, never a raw DB exception.
        raise HTTPException(status_code=404, detail="Not found")

    return SubmitAnswersResponse(answers=[DraftAnswerResponse(**a) for a in merged["answers"]])
