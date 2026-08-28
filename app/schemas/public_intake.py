from pydantic import EmailStr

from app.schemas.common import CamelModel


class IntakeEmailVerifyRequest(CamelModel):
    email: EmailStr


class IntakeSessionResponse(CamelModel):
    session_token: str


class IntakeQuestionResponse(CamelModel):
    """One entry from a link's frozen `questions_snapshot["questions"]` (P2-03
    shape, see app/api/deals.py's create-link route) -- nothing beyond these
    fields, so no deal/answer data can leak through this schema."""

    question_key: str
    prompt: str
    help_text: str | None
    input_type: str
    required: bool
    display_order: int


class IntakeQuestionsResponse(CamelModel):
    org_name: str
    questions: list[IntakeQuestionResponse]
