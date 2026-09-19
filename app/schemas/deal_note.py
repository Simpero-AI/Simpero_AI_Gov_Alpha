from datetime import datetime
from typing import Literal

from app.schemas.common import CamelModel

# A free-text note logged against a deal. Two kinds share one shape and one
# storage path (append-only human_audit_log): "analyst" is a general call /
# meeting note, "interview" is a founder/customer/expert interview note that
# also names the interviewee. Each is its own audit event_type; the list is
# simply every row of that kind, newest first.
DealNoteKind = Literal["analyst", "interview"]


class RecordDealNoteRequest(CamelModel):
    kind: DealNoteKind
    body: str
    # Who was interviewed — only meaningful for kind="interview"; ignored (and
    # returned null) for analyst notes.
    interviewee: str | None = None


class DealNoteResponse(CamelModel):
    kind: DealNoteKind
    body: str
    interviewee: str | None
    actor_email: str | None
    created_at: datetime
