from datetime import datetime

from app.schemas.common import CamelModel

# An analyst's edit to the draft IC memo. Today only the Recommendation is
# editable: the AI-generated draft (icRecommendation.prose) is the default, and
# an analyst can override it with their own text. Stored latest-wins in the
# append-only human_audit_log (event_type "memo_recommendation_saved"), so the
# edit history is preserved and the newest row is the current override.


class RecordMemoRecommendationRequest(CamelModel):
    content: str


class MemoRecommendation(CamelModel):
    content: str
    actor_email: str | None
    created_at: datetime


class MemoDraftResponse(CamelModel):
    """The analyst's saved memo overrides for a deal. `recommendation` is null
    when the analyst hasn't overridden the AI draft yet (the UI then shows the
    generated prose)."""

    recommendation: MemoRecommendation | None
