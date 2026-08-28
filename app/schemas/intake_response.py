from datetime import datetime

from app.schemas.common import CamelModel


class IntakeResponseAnswerResponse(CamelModel):
    """One entry from `deal_intake_response.answers` -> `answers[]`.

    The stored blob is snake_case (`question_key`, not `questionKey`),
    consistent with `mandates.mandate` / `screening_result.rule_results` and
    documented in the implementation brief's "Stored shapes" section.
    CamelModel's `populate_by_name=True` is what lets a stored entry validate
    straight into this model by field name while still serializing camelCase
    on the wire -- so the wire shape and the stored shape stay decoupled and
    neither has to be rewritten if the other moves.

    `prompt` is the wording the external party actually saw, carried in the
    blob rather than joined from `deal_intake_questions`: a platform admin
    can edit or deactivate a question between the link being sent and the
    answer arriving, and the reader needs the question as asked, not as it
    reads today. `answered` is stored explicitly rather than inferred from a
    blank `answer`, so "skipped an optional question" stays distinguishable
    from "answered with whitespace".
    """

    question_key: str
    prompt: str
    answer: str
    answered: bool


class IntakeResponseResponse(CamelModel):
    """P3-05. The org-side read of what the external party submitted -- Step
    3's answers panel (P5-05) and the deal detail page (Q12).

    Nothing here identifies the link's token: `link_id` is deliberately left
    out too, since the org-side reader has no use for it and P3-02 is the
    endpoint for anything about the link itself.
    """

    id: str
    deal_id: str
    respondent_email: str
    submitted_at: datetime | None
    answers: list[IntakeResponseAnswerResponse]
