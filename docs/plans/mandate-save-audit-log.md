# Addendum: audit-log mandate saves

Cross-repo addendum requested from the `Simpero_AI_Gov_Web` frontend session. Written 2026-08-15 by that session as a **plan-only** cross-repo change — no code was written or edited in this repo to produce this doc. To be implemented by this repo's own Claude Code session.

## Problem

`human_audit_log` (`app/models/human_audit_log.py`, via `HumanAuditRepo`) already records product-portal actions like `deal_created` (`app/api/deals.py`, `create_deal`), auth events, uploads, and history deletions — but `PUT /mandate` (`app/api/mandates.py`, `upsert_mandate`) writes no audit entry at all today. Confirmed by reading that file in full: it imports `MandateRepo`/`UserRepo` only, no `HumanAuditRepo`.

The Web frontend's Mandate Builder page has a "Mandate History" drawer (`MandateHistoryDrawer.tsx`) that is currently a hardcoded "History isn't tracked yet" placeholder, explicitly waiting for exactly this. That frontend work is being done separately in the Web repo's own session, reusing the existing `GET /logs/recent-activity` endpoint (already used the same way by the Deal Analysis workspace's `ActivityPane.tsx`) — **no new read endpoint or schema change is needed for this addendum**, only the write side.

## Change

One `HumanAuditRepo(db).append(...)` call added to `upsert_mandate` in `app/api/mandates.py`, immediately after the `MandateRepo(db).upsert(...)` call, following the exact same pattern already used in `app/api/deals.py`'s `create_deal` (lines ~146-154 there):

```python
@router.put("/mandate", response_model=MandateResponse)
async def upsert_mandate(
    body: UpsertMandateRequest,
    claims: dict[str, Any] = Depends(get_claims),
    db: AsyncSession = Depends(get_db),
) -> MandateResponse:
    """Create-or-replace the org's mandate. One row per org (unique org_id);
    user_id just records who last saved it."""
    user = await UserRepo(db).get_by_clerk_id(claims["user_id"])
    assert user is not None  # get_db JIT-provisions this row before the handler runs

    mandate = await MandateRepo(db).upsert(
        {
            "org_id": user.org_id,
            "user_id": user.id,
            "mandate": body.mandate,
        }
    )
    await HumanAuditRepo(db).append(
        {
            "org_id": user.org_id,
            "actor_id": claims["user_id"],
            "actor_email": user.email,
            "event_type": "mandate_saved",
        }
    )
    return MandateResponse(mandate=mandate.mandate or [], updated_at=mandate.updated_at)
```

Add `from app.repo.HumanAuditRepo import HumanAuditRepo` to the file's existing import block (alongside `MandateCategoryRepo`/`MandateOptionsRepo`/`MandateRepo`/`UserRepo`).

`event_type` is `"mandate_saved"` — one flat string, no distinction between a first-time create and a subsequent update, matching how `MandateRepo.upsert` itself treats both the same way (a single create-or-replace operation, not two code paths). `deal_id`/`session_id`/`payload` are left `null` — this event isn't deal- or session-scoped, and there's nothing meaningful to put in `payload` beyond what `event_type` + `actor_email` + `created_at` already say (the mandate's own content is queryable via `GET /mandate` if ever needed; duplicating it into the audit payload would be a second source of truth for no reader that needs it yet).

`user.email` — confirm this attribute name matches `UserRepo`'s model (it's used the same way via `_actor()` in `app/api/deals.py:42-48`, `user.email`, so it should be identical here; this file just doesn't have a shared `_actor()` helper of its own — don't add one for a single call site, inline it as above).

## Verification

Manually or via a quick test: call `PUT /mandate` (already exercised by the existing Web frontend integration), then confirm a new `human_audit_log` row appears with `event_type = 'mandate_saved'` for the calling org, and that `GET /logs/recent-activity` (unchanged) returns it in its `rows` array with `action: "mandate_saved"`.

## Out of scope

No new endpoint, no schema change on `ActivityRowResponse`/`RecentActivityResponse` (`app/schemas/logs.py`) — the Web session is deliberately not surfacing "who" saved it (actor_email isn't in that response schema today), only "that a save happened and when," to avoid a schema change beyond what was asked. If "who" is wanted in the drawer later, that's a small follow-up: add `actor_email: str | None` to `ActivityRowResponse` and `logs.py`'s row construction — not part of this addendum.
