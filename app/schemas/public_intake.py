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


class AnswerInput(CamelModel):
    """Client-supplied half of one answer -- `prompt`/`answered` are never
    accepted from the client, only derived server-side from the link's
    questions_snapshot (see POST /public/intake/answers)."""

    question_key: str
    answer: str


class SubmitAnswersRequest(CamelModel):
    answers: list[AnswerInput]


class DraftAnswerResponse(CamelModel):
    question_key: str
    prompt: str
    answer: str
    answered: bool


class SubmitAnswersResponse(CamelModel):
    """Echoes the current merged draft state after this call's overlay --
    minimal, matches this codebase's CamelModel wire convention."""

    answers: list[DraftAnswerResponse]
