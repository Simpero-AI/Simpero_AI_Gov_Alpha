from datetime import UTC, datetime

from app.models.deal_intake_link import DealIntakeLink


def compute_intake_link_effective_status(link: DealIntakeLink) -> str:
    """Read-only lazy-expiry check -- a `pending` link whose `expires_at` has
    passed is *effectively* expired even before anything writes
    `status='expired'` to the row. Never writes to the DB (no session
    parameter, ever); callers that need the terminal status persisted go
    through IntakeLinkRepo.mark_expired + an explicit flush instead. Shared
    by P3-01/02/06/14 -- keep this module path and function name stable."""
    if link.status == "pending" and link.expires_at <= datetime.now(UTC):
        return "expired"
    return link.status
