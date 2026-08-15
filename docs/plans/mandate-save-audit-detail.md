# Addendum: who + what for mandate-save audit entries

Cross-repo addendum requested from the `Simpero_AI_Gov_Web` frontend session. Written 2026-08-15 by that session as a **plan-only** cross-repo change — no code was written or edited in this repo to produce this doc. To be implemented by this repo's own Claude Code session. Builds on the already-shipped `docs/plans/mandate-save-audit-log.md` (the base `mandate_saved` event write) — that one is done and working; this is additive.

## Problem

Since a mandate is one row per org and any org member can save it, the frontend's Mandate History drawer needs to show **who** made the last change and **what** changed, not just that a save happened. Two gaps today:

1. `HumanAuditRepo.append(...)` in `upsert_mandate` (`app/api/mandates.py`) already writes `actor_email` on every `mandate_saved` row — but `GET /logs/recent-activity` (`app/api/logs.py`, `app/schemas/logs.py`) never reads it back out. `ActivityRowResponse` has no `actor_email` field.
2. Nothing computes or stores *what* changed. The audit row has an empty `payload` today.

## Change

### 1. Compute a diff at save time, store it in the audit row's `payload`

In `upsert_mandate` (`app/api/mandates.py`), fetch the org's *previous* mandate before upserting, diff it against the incoming `body.mandate`, and pass the diff as `payload` on the existing `HumanAuditRepo.append(...)` call (which already runs right after the upsert per the base addendum).

```python
@router.put("/mandate", response_model=MandateResponse)
async def upsert_mandate(
    body: UpsertMandateRequest,
    claims: dict[str, Any] = Depends(get_claims),
    db: AsyncSession = Depends(get_db),
) -> MandateResponse:
    user = await UserRepo(db).get_by_clerk_id(claims["user_id"])
    assert user is not None

    previous = await MandateRepo(db).get_for_org()
    diff = _diff_mandate(previous.mandate if previous else [], body.mandate)

    mandate = await MandateRepo(db).upsert(
        {"org_id": user.org_id, "user_id": user.id, "mandate": body.mandate}
    )
    await HumanAuditRepo(db).append(
        {
            "org_id": user.org_id,
            "actor_id": claims["user_id"],
            "actor_email": user.email,
            "event_type": "mandate_saved",
            "payload": diff,
        }
    )
    return MandateResponse(mandate=mandate.mandate or [], updated_at=mandate.updated_at)
```

`_diff_mandate` is a new, pure, private helper in the same file (no new module needed for ~30 lines). It operates on the raw `list[Any]` shape the Web frontend already sends/stores (see that repo's `src/api/mandate.ts`/`src/lib/mandateSelection.ts` for the authoritative shape — this backend never validates it, so the diff must tolerate any entry missing expected keys rather than raising):

```python
def _diff_mandate(old: list[Any], new: list[Any]) -> list[dict[str, Any]]:
    """Entry-level diff, keyed by each item's own `category` string (the
    same denormalized join key the Web frontend already uses — see that
    repo's mandateSelection.ts). Two entry shapes exist: category+options
    (`options: [{option, option_id, sub_options?}]`) and Check Size Range
    (`min`, `max`, no `options` key). Produces one diff entry per category
    that actually changed; unchanged categories are omitted entirely, not
    included with empty diffs -- an empty overall list means "no real
    change" (e.g. Save clicked with nothing edited)."""
    old_by_cat = {item.get("category"): item for item in old if item.get("category")}
    new_by_cat = {item.get("category"): item for item in new if item.get("category")}
    diffs: list[dict[str, Any]] = []

    for category in sorted(set(old_by_cat) | set(new_by_cat)):
        old_item, new_item = old_by_cat.get(category), new_by_cat.get(category)

        if (old_item and "options" in old_item) or (new_item and "options" in new_item):
            old_opts = {o["option"] for o in (old_item or {}).get("options", [])}
            new_opts = {o["option"] for o in (new_item or {}).get("options", [])}
            added, removed = sorted(new_opts - old_opts), sorted(old_opts - new_opts)

            sub_added, sub_removed = [], []
            for opt in (new_item or {}).get("options", []):
                old_opt = next(
                    (o for o in (old_item or {}).get("options", []) if o["option"] == opt["option"]), None
                )
                old_subs = {s["option"] for s in (old_opt or {}).get("sub_options", [])}
                new_subs = {s["option"] for s in opt.get("sub_options", [])}
                if new_subs - old_subs:
                    sub_added.append({"option": opt["option"], "subOptions": sorted(new_subs - old_subs)})
                if old_subs - new_subs:
                    sub_removed.append({"option": opt["option"], "subOptions": sorted(old_subs - new_subs)})

            if added or removed or sub_added or sub_removed:
                diffs.append({
                    "category": category,
                    "type": "options",
                    **({"added": added} if added else {}),
                    **({"removed": removed} if removed else {}),
                    **({"subOptionsAdded": sub_added} if sub_added else {}),
                    **({"subOptionsRemoved": sub_removed} if sub_removed else {}),
                })
        else:
            old_min, old_max = (old_item or {}).get("min"), (old_item or {}).get("max")
            new_min, new_max = (new_item or {}).get("min"), (new_item or {}).get("max")
            if (old_min, old_max) != (new_min, new_max):
                diffs.append({
                    "category": category,
                    "type": "range",
                    "from": {"min": old_min, "max": old_max},
                    "to": {"min": new_min, "max": new_max},
                })

    return diffs
```

This is deliberately a same-request read-then-write (fetch previous, then upsert) rather than a DB trigger or a separate versioning table — `mandates` already stores only the current row (no history table exists, and building one is out of scope), so "the previous value" only exists in the instant before this request's upsert runs. No transaction/locking concern beyond what `MandateRepo.upsert`'s existing `ON CONFLICT DO UPDATE` already provides — a lost-update race between two concurrent saves would only affect the computed diff's accuracy for one of the two audit rows, not data integrity of the mandate itself.

### 2. Expose `actor_email` and `payload` through `GET /logs/recent-activity`

`app/schemas/logs.py`:

```python
class ActivityRowResponse(CamelModel):
    id: str
    created_at: datetime
    action: str
    session_id: str | None
    job_id: str | None
    actor_email: str | None = None
    payload: Any | None = None
```

`app/api/logs.py`'s row construction gains two fields, reading straight off `row.actor_email`/`row.payload` (both already columns on `HumanAuditLog`, already populated by every existing writer in #2's call-site list — this is purely additive, no writer needs to change):

```python
ActivityRowResponse(
    id=str(row.id),
    created_at=row.created_at,
    action=row.event_type,
    session_id=str(row.session_id) if row.session_id else None,
    job_id=None,
    actor_email=row.actor_email,
    payload=row.payload,
)
```

`payload: Any | None` (not a typed union) because this endpoint is generic across every `event_type` in the system (`deal_created`, `auth_login`, `admin_mandate_option_created`, etc.) — most of which don't set a payload today and none of which share a schema. Typing it per-event-type here would require this shared endpoint to know about every consumer's shape; the Web frontend already treats loosely-typed JSONB payloads as untyped/interpreted-by-the-specific-consumer elsewhere (the `mandates.mandate` column itself), so this follows the same precedent.

## Verification

Save a mandate twice with different selections (e.g. add "Series A" to Investment Stage, then in a second save remove it and add "Canada" to Geographies with a "British Columbia" sub-option). Confirm `GET /logs/recent-activity` returns two `mandate_saved` rows, each with the correct `actorEmail` and a `payload` diff matching only what actually changed in that save — the first row's payload should show `{category: "Investment Stage", type: "options", added: ["Series A"]}`, the second should show the Investment Stage removal and the Geographies addition with `subOptionsAdded`, and neither should mention Check Size Range or any category untouched in that particular save.

## Out of scope

No mandate version-history table (this reads audit-log payloads, not a proper versioned snapshot store — reconstructing "the mandate as of time T" from a chain of diffs is not built here). No diff for the legacy `investment_profile.mandate` blob fields (Hold Period, Target Return, ESG, Special Notes, Financial Thresholds) — those aren't part of `PUT /mandate`'s body at all. No change to any other event_type's audit payload.
