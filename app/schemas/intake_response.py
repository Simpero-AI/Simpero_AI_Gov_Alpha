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

    `submitted_at` stays `datetime | None`, matching its nullable column, and
    the Web contract widens to meet it rather than the reverse. Review raised
    that Simpero_AI_Gov_Web's `IntakeResponse` types `submittedAt: string`
    (non-nullable) while this admits None. NULL is unreachable through today's
    writer -- P3-11's submit_intake always sets `func.now()`, and this route
    404s until a submit has happened -- so either side could be made to agree.
    Tightening this one to non-optional was rejected because it would
    manufacture exactly the failure this endpoint just removed one level in:
    `deal_intake_response` is insert-only at the grant layer, so a row that
    somehow landed with a NULL `submitted_at` could never be repaired, and a
    non-optional field would 500 that deal's Step 3 panel permanently. The
    precedent for the other direction is already in the same Web file --
    `IntakeLink.submittedAt` is `string | null` there, and `createdAt` is
    optional-and-nullable rendered as an em dash -- so `IntakeResponse` is the
    outlier, not this. Tracked as a Web-side follow-up on P5-05.
    """

    id: str
    deal_id: str
    respondent_email: str
    submitted_at: datetime | None
    answers: list[IntakeResponseAnswerResponse]
