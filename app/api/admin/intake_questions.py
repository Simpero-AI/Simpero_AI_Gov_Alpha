from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_dependencies import _admin_actor, get_admin_db, require_platform_admin
from app.repo.DealIntakeQuestionRepo import DealIntakeQuestionRepo
from app.repo.HumanAuditRepo import HumanAuditRepo
from app.schemas.admin.intake_question import (
    CreateIntakeQuestionRequest,
    DealIntakeQuestionResponse,
    ReorderIntakeQuestionsRequest,
    UpdateIntakeQuestionRequest,
)

# Platform-admin-only CRUD for the deal_intake_questions global reference
# table (no org_id, no RLS -- see app/models/deal_intake_question.py).
# Structurally a copy of app/api/admin/mandates.py (Q14). "Delete" is
# activate/deactivate, never a DB DELETE: a link's questions_snapshot holds
# its own copy of a question's text, so deactivating the source row here
# cannot touch a past snapshot -- it only removes the question from what
# list_active() (P2-03) offers for a *new* link going forward.
router = APIRouter(prefix="/intake-questions", tags=["admin"])


def _response(question: Any) -> DealIntakeQuestionResponse:
    return DealIntakeQuestionResponse(
        id=str(question.id),
        question_key=question.question_key,
        prompt=question.prompt,
        help_text=question.help_text,
        input_type=question.input_type,
        required=question.required,
        display_order=question.display_order,
        is_active=question.is_active,
    )


@router.get("", response_model=list[DealIntakeQuestionResponse])
async def list_questions(
    claims: dict[str, Any] = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_admin_db),
) -> list[DealIntakeQuestionResponse]:
    """Active and inactive alike -- an admin has to see a deactivated
    question to reactivate it."""
    questions = await DealIntakeQuestionRepo(db).list_all()
    return [_response(question) for question in questions]


@router.post("", response_model=DealIntakeQuestionResponse, status_code=201)
async def create_question(
    payload: CreateIntakeQuestionRequest,
    claims: dict[str, Any] = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_admin_db),
) -> DealIntakeQuestionResponse:
    repo = DealIntakeQuestionRepo(db)
    display_order = await repo.next_display_order()
    question = await repo.create(
        {
            "question_key": payload.question_key,
            "prompt": payload.prompt,
            "help_text": payload.help_text,
            "input_type": payload.input_type,
            "required": payload.required,
            "display_order": display_order,
        }
    )
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="An active question with this key already exists"
        ) from exc

    org_id, actor_id, actor_email = await _admin_actor(db, claims)
    await HumanAuditRepo(db).append(
        {
            "org_id": org_id,
            "actor_id": actor_id,
            "actor_email": actor_email,
            "event_type": "admin_intake_question_created",
            "payload": {"question_id": str(question.id), "question_key": question.question_key},
        }
    )
    return _response(question)


@router.patch("/{question_id}", response_model=DealIntakeQuestionResponse)
async def update_question(
    question_id: UUID,
    payload: UpdateIntakeQuestionRequest,
    claims: dict[str, Any] = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_admin_db),
) -> DealIntakeQuestionResponse:
    question = await DealIntakeQuestionRepo(db).update(
        question_id,
        {
            "prompt": payload.prompt,
            "help_text": payload.help_text,
            "input_type": payload.input_type,
            "required": payload.required,
        },
    )
    if question is None:
        raise HTTPException(status_code=404, detail="Intake question not found")
    await db.flush()

    org_id, actor_id, actor_email = await _admin_actor(db, claims)
    await HumanAuditRepo(db).append(
        {
            "org_id": org_id,
            "actor_id": actor_id,
            "actor_email": actor_email,
            "event_type": "admin_intake_question_updated",
            "payload": {"question_id": str(question.id)},
        }
    )
    return _response(question)


@router.put("/reorder", response_model=list[DealIntakeQuestionResponse])
async def reorder_questions(
    payload: ReorderIntakeQuestionsRequest,
    claims: dict[str, Any] = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_admin_db),
) -> list[DealIntakeQuestionResponse]:
    """Whole-list reorder (Q7: flat list, whole active set applies) -- the
    submitted id set must exactly match the current table, so there's never
    a partial reorder to reconcile against concurrent create/delete."""
    repo = DealIntakeQuestionRepo(db)
    current = await repo.list_all()
    current_by_id = {str(question.id): question for question in current}

    if set(payload.question_ids) != set(current_by_id.keys()):
        raise HTTPException(
            status_code=422,
            detail="question_ids must contain exactly the current set of question ids",
        )

    for position, question_id in enumerate(payload.question_ids):
        current_by_id[question_id].display_order = position
    await db.flush()

    org_id, actor_id, actor_email = await _admin_actor(db, claims)
    await HumanAuditRepo(db).append(
        {
            "org_id": org_id,
            "actor_id": actor_id,
            "actor_email": actor_email,
            "event_type": "admin_intake_question_reordered",
            "payload": {"question_ids": payload.question_ids},
        }
    )
    ordered = sorted(current_by_id.values(), key=lambda question: question.display_order)
    return [_response(question) for question in ordered]


async def _set_active(
    question_id: UUID,
    is_active: bool,
    event_type: str,
    claims: dict[str, Any],
    db: AsyncSession,
) -> DealIntakeQuestionResponse:
    repo = DealIntakeQuestionRepo(db)
    question = await repo.set_active(question_id, is_active)
    if question is None:
        raise HTTPException(status_code=404, detail="Intake question not found")
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="An active question with this key already exists"
        ) from exc

    org_id, actor_id, actor_email = await _admin_actor(db, claims)
    await HumanAuditRepo(db).append(
        {
            "org_id": org_id,
            "actor_id": actor_id,
            "actor_email": actor_email,
            "event_type": event_type,
            "payload": {"question_id": str(question.id)},
        }
    )
    return _response(question)


@router.patch("/{question_id}/activate", response_model=DealIntakeQuestionResponse)
async def activate_question(
    question_id: UUID,
    claims: dict[str, Any] = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_admin_db),
) -> DealIntakeQuestionResponse:
    return await _set_active(question_id, True, "admin_intake_question_activated", claims, db)


@router.patch("/{question_id}/deactivate", response_model=DealIntakeQuestionResponse)
async def deactivate_question(
    question_id: UUID,
    claims: dict[str, Any] = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_admin_db),
) -> DealIntakeQuestionResponse:
    """Soft toggle only -- see the module docstring. Never deletes the row,
    so any link that already snapshotted this question's text is
    untouched (acceptance criteria)."""
    return await _set_active(question_id, False, "admin_intake_question_deactivated", claims, db)
