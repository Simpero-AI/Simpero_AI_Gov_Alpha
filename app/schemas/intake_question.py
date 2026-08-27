from app.schemas.common import CamelModel


class IntakeQuestionResponse(CamelModel):
    """Product-side shape -- deliberately slimmer than the admin portal's
    DealIntakeQuestionResponse (app/schemas/admin/intake_question.py): no
    is_active (this endpoint only ever returns active rows, so the field
    would always be true), matching the product/admin split already used
    for mandates (app/schemas/mandate.py vs app/schemas/admin/mandate.py)."""

    id: str
    question_key: str
    prompt: str
    help_text: str | None = None
    input_type: str
    required: bool
    display_order: int
