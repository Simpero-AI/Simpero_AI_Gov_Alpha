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


def compute_pipeline_intake_status(link: DealIntakeLink | None) -> str:
    """P3-06. Collapses a deal's most recent link into the three states the
    Live Pipeline grid routes on (F4/D3): `pending` sends the row to the
    waiting panel, `submitted` sends it to Step 3, `none` leaves it on
    today's normal analysis route.

    Everything that is not live-and-waiting or actually-submitted collapses
    to `none` -- no link row at all, a revoked one, an expired one, and (via
    compute_intake_link_effective_status) one still stored `pending` but
    past its `expires_at`. The grid deliberately has no fourth state: a link
    that is functionally dead should route exactly like a deal that never
    had one, so the org user is never sent to a waiting panel to wait for
    something that can no longer arrive.
    """
    if link is None:
        return "none"
    effective = compute_intake_link_effective_status(link)
    return effective if effective in ("pending", "submitted") else "none"
