from datetime import datetime
from typing import Literal

from pydantic import EmailStr

from app.schemas.common import CamelModel


class CreateIntakeLinkRequest(CamelModel):
    recipient_email: EmailStr


class CreateIntakeLinkResponse(CamelModel):
    id: str
    token: str  # raw token -- appears ONLY in this response, never again
    status: str
    expires_at: datetime


# Mirrors deal_intake_link's ck_deal_intake_link_status CHECK constraint and
# the model's _STATUSES tuple. Read-only here -- this endpoint never writes
# status -- but declaring it as a Literal keeps the OpenAPI schema honest for
# the wizard's Step 2 panel (P5-04) and the grid routing (P3-06/P5-07), which
# both branch on these exact strings.
IntakeLinkStatus = Literal["pending", "submitted", "revoked", "expired"]


class IntakeLinkStatusResponse(CamelModel):
    """P3-02. The org-side status read for the wizard's Step 2 waiting panel.

    Deliberately exactly four fields: the raw token is returned once by P3-01
    and never again, and `token_hash` must not leak here under any status --
    the acceptance criterion is a property of this class's field list, so do
    not widen it without re-reading that.

    `status` is the *effective* status from
    compute_intake_link_effective_status, not `deal_intake_link.status` as
    stored: a row still stored `pending` past its `expires_at` reads
    `expired` here, because P3-01's lazy-expire write only runs on the next
    generate call and the panel must not lag behind it.
    """

    status: IntakeLinkStatus
    recipient_email: str
    expires_at: datetime
    submitted_at: datetime | None
