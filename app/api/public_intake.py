import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthenticationError
from app.core.intake_security import (
    IntakeSessionClaims,
    decode_intake_session_jwt,
    encode_intake_session_jwt,
)
from app.core.public_dependencies import get_public_link_db, get_public_session_db
from app.core.rate_limit_middleware import _client_ip
from app.models.deal_intake_link import DealIntakeLink
from app.models.deal_intake_response import DealIntakeResponse
from app.models.organisation import Organisation
from app.repo.DataSourceRepo import DataSourceRepo
from app.repo.HumanAuditRepo import HumanAuditRepo
from app.repo.IntakeLinkRepo import IntakeLinkRepo
from app.schemas.public_intake import (
    AnswerInput,
    DraftAnswerResponse,
    IntakeEmailVerifyRequest,
    IntakeQuestionResponse,
    IntakeQuestionsResponse,
    IntakeSessionResponse,
    IntakeSubmitResponse,
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


async def _decode_claims(session_token: str) -> IntakeSessionClaims:
    """Duplicated from app/api/public_uploads.py -- see that file's own
    docstring for why this is copied rather than imported across router
    files (this codebase's existing precedent for small router-local
    helpers, e.g. _org_name_for_link above)."""
    try:
        return decode_intake_session_jwt(session_token)
    except AuthenticationError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc


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


@router.post("/submit", response_model=IntakeSubmitResponse)
async def submit_intake(
    request: Request,
    session_and_link: tuple[AsyncSession, DealIntakeLink] = Depends(get_public_session_db),
    claims: IntakeSessionClaims = Depends(_decode_claims),
) -> IntakeSubmitResponse:
    db, _ = session_and_link

    # Row-locked reload keyed on the verified session claim, not the
    # dependency's own (unlocked) `link` -- closes the concurrent-double-
    # submit race, see IntakeLinkRepo.get_pending_by_id_for_update's docstring.
    link = await IntakeLinkRepo(db).get_pending_by_id_for_update(claims.link_id)
    if link is None:
        raise HTTPException(status_code=404, detail="Not found")

    # Completeness gate: every required question must be answered in the
    # draft. draft_answers is None if the recipient never called /answers.
    required_keys = {
        q["question_key"]
        for q in (link.questions_snapshot or {}).get("questions", [])
        if q["required"]
    }
    answered_keys = {
        a["question_key"] for a in (link.draft_answers or {}).get("answers", []) if a["answered"]
    }
    missing = required_keys - answered_keys
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Required questions not answered: {sorted(missing)}",
        )

    # Document gate: at least one pending/verified upload tied to this link.
    doc_count = await DataSourceRepo(db).count_for_intake_link_by_status(link.id)
    if doc_count == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="At least one document must be uploaded before submitting",
        )

    ip = _client_ip(request)
    ip_address = None if ip == "unknown" else ip
    user_agent = request.headers.get("user-agent")

    # Insert while link.status is still 'pending' in the DB -- required by
    # intake_response_insert's WITH CHECK (see
    # b4f8e1c3a962_intake_keyhole_policies.py). The explicit flush here is
    # load-bearing, not just an optimization: SQLAlchemy's unit-of-work does
    # NOT guarantee this INSERT is emitted before the status UPDATE below
    # just because db.add() was called first in code -- without forcing it,
    # the UPDATE can be flushed first, flipping status to 'submitted' before
    # the INSERT runs, which the WITH CHECK then rejects.
    db.add(
        DealIntakeResponse(
            org_id=link.org_id,
            deal_id=link.deal_id,
            link_id=link.id,
            respondent_email=claims.email,
            answers=link.draft_answers,
            submitted_at=func.now(),
            ip_address=ip_address,
            user_agent=user_agent,
        )
    )
    await db.flush()

    # Flip status in place on the already-locked ORM object -- SQLAlchemy
    # emits the UPDATE on flush, no separate UPDATE statement.
    link.status = "submitted"
    link.submitted_at = func.now()

    await HumanAuditRepo(db).append(
        {
            "org_id": link.org_id,
            "actor_id": None,
            "actor_email": claims.email,
            "event_type": "intake_submitted",
            "deal_id": link.deal_id,
            "payload": {"link_id": str(link.id), "document_count": doc_count},
            "ip_address": ip_address,
            "user_agent": user_agent,
        }
    )

    await db.flush()
    # The UPDATE above has no RETURNING (AsyncSession doesn't auto-backfill
    # func.now() on UPDATE the way it does on INSERT), so link.submitted_at
    # is still unpopulated in memory -- an explicit refresh is required here;
    # accessing the attribute directly would trigger an implicit lazy-load
    # outside the async greenlet and raise MissingGreenlet.
    await db.refresh(link, attribute_names=["submitted_at"])
    submitted_at = link.submitted_at
    assert submitted_at is not None  # just set via func.now() above

    return IntakeSubmitResponse(submitted=True, submitted_at=submitted_at)
