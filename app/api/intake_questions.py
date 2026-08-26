from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_db
from app.repo.DealIntakeQuestionRepo import DealIntakeQuestionRepo
from app.schemas.intake_question import IntakeQuestionResponse

# Read-only product-side lookup -- the active question set an org user's
# Step 1 (P3-01) snapshots at link-generation time. Management (create/
# edit/reorder/activate/deactivate) is the admin portal's job
# (app/api/admin/intake_questions.py, P2-02); this router is the sole
# read path on the product side, same split as app/api/mandates.py vs
# app/api/admin/mandates.py. No auth beyond the normal product
# Depends(get_db) -- every authenticated org user can read the question
# set, there's nothing tenant-specific about it (global reference table).
router = APIRouter(tags=["intake-questions"])


def _response(question: Any) -> IntakeQuestionResponse:
    return IntakeQuestionResponse(
        id=str(question.id),
        question_key=question.question_key,
        prompt=question.prompt,
        help_text=question.help_text,
        input_type=question.input_type,
        required=question.required,
        display_order=question.display_order,
    )


@router.get("/intake-questions", response_model=list[IntakeQuestionResponse])
async def list_intake_questions(
    db: AsyncSession = Depends(get_db),
) -> list[IntakeQuestionResponse]:
    questions = await DealIntakeQuestionRepo(db).list_active()
    return [_response(question) for question in questions]
