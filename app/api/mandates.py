from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_claims, get_db
from app.repo.HumanAuditRepo import HumanAuditRepo
from app.repo.MandateCategoryRepo import MandateCategoryRepo
from app.repo.MandateOptionsRepo import MandateOptionsRepo
from app.repo.MandateRepo import MandateRepo
from app.repo.UserRepo import UserRepo
from app.schemas.mandate import (
    MandateCategoryResponse,
    MandateOptionResponse,
    MandateResponse,
    UpsertMandateRequest,
)

router = APIRouter(tags=["mandates"])


def _mandate_item_key(item: dict[str, Any]) -> str | None:
    """`category_id` when present -- the mandate_categories.id UUID the Web
    frontend's toMandateItems() has always attached to every saved item
    (src/lib/mandateSelection.ts), so this is already flowing in every real
    payload today, no frontend change needed. Falls back to `slug` (the
    same stable per-category identity MandateCategoryResponse.slug exposes,
    app/models/mandate.py, once the frontend starts sending it per entry
    too), then to the `category` display-name string as a last resort for
    malformed/legacy entries missing both. Keying on either stable id is
    what actually fixes the rename-breaks-the-diff failure mode: text-only
    keying treats an admin rename between two saves as "remove everything
    from the old name, add everything under the new name" for every entry
    the user never touched, since the previously-saved item is frozen with
    whatever text was current at save time."""
    return item.get("category_id") or item.get("slug") or item.get("category")


def _diff_mandate(old: list[Any], new: list[Any]) -> list[dict[str, Any]]:
    """Entry-level diff, keyed by `_mandate_item_key` (category_id, then
    slug, then the `category` display-name string -- see its docstring).
    Two entry shapes exist: category+options (`options: [{option,
    option_id, sub_options?}]`) and Check Size Range (`min`, `max`, no
    `options` key). Produces one diff entry per category that actually
    changed; unchanged categories are omitted entirely, not included with
    empty diffs -- an empty overall list means "no real change" (e.g. Save
    clicked with nothing edited). The emitted `category` field is always
    the current display name (new item's if present, else old's), not the
    join key -- a diff should read as a name even when it was matched by
    id."""
    old_by_key: dict[str, Any] = {
        key: item for item in old if (key := _mandate_item_key(item)) is not None
    }
    new_by_key: dict[str, Any] = {
        key: item for item in new if (key := _mandate_item_key(item)) is not None
    }
    diffs: list[dict[str, Any]] = []

    for key in sorted(set(old_by_key) | set(new_by_key)):
        old_item, new_item = old_by_key.get(key), new_by_key.get(key)
        category = (new_item or old_item or {}).get("category", key)

        if (old_item and "options" in old_item) or (new_item and "options" in new_item):
            old_opts = {o["option"] for o in (old_item or {}).get("options", [])}
            new_opts = {o["option"] for o in (new_item or {}).get("options", [])}
            added, removed = sorted(new_opts - old_opts), sorted(old_opts - new_opts)

            sub_added, sub_removed = [], []
            for opt in (new_item or {}).get("options", []):
                old_opt = next(
                    (
                        o
                        for o in (old_item or {}).get("options", [])
                        if o["option"] == opt["option"]
                    ),
                    None,
                )
                old_subs = {s["option"] for s in (old_opt or {}).get("sub_options", [])}
                new_subs = {s["option"] for s in opt.get("sub_options", [])}
                if new_subs - old_subs:
                    sub_added.append(
                        {"option": opt["option"], "subOptions": sorted(new_subs - old_subs)}
                    )
                if old_subs - new_subs:
                    sub_removed.append(
                        {"option": opt["option"], "subOptions": sorted(old_subs - new_subs)}
                    )

            if added or removed or sub_added or sub_removed:
                diffs.append(
                    {
                        "category": category,
                        "type": "options",
                        **({"added": added} if added else {}),
                        **({"removed": removed} if removed else {}),
                        **({"subOptionsAdded": sub_added} if sub_added else {}),
                        **({"subOptionsRemoved": sub_removed} if sub_removed else {}),
                    }
                )
        else:
            old_min, old_max = (old_item or {}).get("min"), (old_item or {}).get("max")
            new_min, new_max = (new_item or {}).get("min"), (new_item or {}).get("max")
            if (old_min, old_max) != (new_min, new_max):
                diffs.append(
                    {
                        "category": category,
                        "type": "range",
                        "from": {"min": old_min, "max": old_max},
                        "to": {"min": new_min, "max": new_max},
                    }
                )

    return diffs


def _nest_options_by_category(
    options: list[Any],
) -> dict[UUID, list[MandateOptionResponse]]:
    """Two-pass: build a response node per row, then thread each into its
    parent's sub_options (or its category's top-level bucket). Rows arrive
    sorted by `option` (see MandateOptionsRepo.list_all), so both levels
    come out alphabetical for free. A parent_option_id pointing at a row
    outside this set can't happen with list_all(), but is treated as
    top-level rather than raising, just in case."""
    nodes: dict[UUID, MandateOptionResponse] = {
        option.id: MandateOptionResponse(id=str(option.id), option=option.option, sub_options=[])
        for option in options
    }
    buckets: dict[UUID, list[MandateOptionResponse]] = {}
    for option in options:
        node = nodes[option.id]
        parent = nodes.get(option.parent_option_id) if option.parent_option_id else None
        if parent is not None:
            parent.sub_options.append(node)
        else:
            buckets.setdefault(option.category_id, []).append(node)
    return buckets


@router.get("/mandate-categories", response_model=list[MandateCategoryResponse])
async def list_mandate_categories(
    db: AsyncSession = Depends(get_db),
) -> list[MandateCategoryResponse]:
    """Read-only lookup for the product portal -- categories and their
    options are managed on the admin portal (platform admins only)."""
    categories = await MandateCategoryRepo(db).list()
    options_by_category = _nest_options_by_category(await MandateOptionsRepo(db).list_all())
    return [
        MandateCategoryResponse(
            id=str(category.id),
            category=category.category,
            slug=category.slug,
            options=options_by_category.get(category.id, []),
        )
        for category in categories
    ]


@router.get("/mandate", response_model=MandateResponse | None)
async def get_mandate(db: AsyncSession = Depends(get_db)) -> MandateResponse | None:
    """The org's own mandate -- null (never 404) when it hasn't been set
    yet, same idiom as GET /investment-profile."""
    mandate = await MandateRepo(db).get_for_org()
    if mandate is None:
        return None
    return MandateResponse(mandate=mandate.mandate or [], updated_at=mandate.updated_at)


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

    previous = await MandateRepo(db).get_for_org()
    diff = _diff_mandate((previous.mandate or []) if previous else [], body.mandate)

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
            "payload": diff,
        }
    )
    return MandateResponse(mandate=mandate.mandate or [], updated_at=mandate.updated_at)
