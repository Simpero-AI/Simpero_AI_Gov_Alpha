from datetime import datetime
from typing import Literal

from app.schemas.common import CamelModel

# The investment-committee decision on a deal. Append-only in human_audit_log
# (event_type "ic_sign_off"); the latest row is the current decision.
IcSignOffDecision = Literal["approve", "decline"]


class RecordIcSignOffRequest(CamelModel):
    decision: IcSignOffDecision
    notes: str | None = None


class IcSignOffResponse(CamelModel):
    """The current IC decision for a deal (the latest ic_sign_off audit event),
    or the endpoint returns null when none has been recorded yet."""

    decision: IcSignOffDecision
    notes: str | None
    actor_email: str | None
    created_at: datetime
