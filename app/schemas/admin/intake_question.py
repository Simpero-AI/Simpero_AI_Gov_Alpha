from typing import Literal

from pydantic import Field

from app.schemas.common import CamelModel

# Mirrors the DB-level ck_deal_intake_questions_input_type CHECK constraint
# (app/models/deal_intake_question.py) -- constrained here too so a bad
# value gets a 422 at the request boundary instead of reaching the DB at
# all. Matters more than usual for this column specifically: a link's
# questions_snapshot freezes input_type at generation time, so a bad value
# doesn't just fail on read, it gets baked into every link issued while
# the row is active.
IntakeQuestionInputType = Literal["text", "textarea"]


class DealIntakeQuestionResponse(CamelModel):
    id: str
    question_key: str
    prompt: str
    help_text: str | None = None
    input_type: str
    required: bool
    display_order: int
    is_active: bool


class CreateIntakeQuestionRequest(CamelModel):
    """question_key is immutable once set -- there is no UpdateIntakeQuestionRequest
    field for it. display_order is not accepted here: a new question is
    always appended (see next_display_order); use the reorder endpoint to
    move it."""

    question_key: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=500)
    help_text: str | None = None
    input_type: IntakeQuestionInputType
    required: bool = False


class UpdateIntakeQuestionRequest(CamelModel):
    """Every editable field except question_key (immutable) and is_active
    (its own activate/deactivate routes, so a PATCH here can't be used to
    silently flip visibility as a side effect of an unrelated edit)."""

    prompt: str = Field(min_length=1, max_length=500)
    help_text: str | None = None
    input_type: IntakeQuestionInputType
    required: bool


class ReorderIntakeQuestionsRequest(CamelModel):
    """The full ordered list of question ids, in their new display order --
    a flat list gets reordered as a whole (Q7), not moved one step at a
    time, so there's no partial-order state to reconcile."""

    question_ids: list[str]
