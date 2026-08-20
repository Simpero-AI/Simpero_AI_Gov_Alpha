from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_dependencies import _admin_actor, get_admin_db, require_platform_admin
from app.repo.HumanAuditRepo import HumanAuditRepo
from app.repo.MandateCategoryRepo import MandateCategoryRepo
from app.repo.MandateOptionsRepo import MandateOptionsRepo
from app.schemas.admin.mandate import (
    CreateMandateCategoryRequest,
    CreateMandateOptionRequest,
    CreateMandateSubOptionRequest,
    MandateCategoryResponse,
    MandateOptionResponse,
    UpdateMandateCategoryRequest,
    UpdateMandateOptionRequest,
)
from app.schemas.common import SuccessResponse

# Platform-admin-only CRUD for the mandate_categories/mandate_options global
# reference tables (no org_id, no RLS -- see app/models/mandate.py). The
# product portal only ever reads these (app/api/mandates.py); this router
# is the sole write path.
router = APIRouter(prefix="/mandates", tags=["admin"])


def _nest_options_by_category(
    options: list[Any],
) -> dict[UUID, list[MandateOptionResponse]]:
    """Two-pass: build a response node per row, then thread each into its
    parent's sub_options (or its category's top-level bucket). Rows arrive
    sorted by `option`, so both levels come out alphabetical for free. A
    parent_option_id pointing outside this set is treated as top-level
    rather than raising."""
    nodes: dict[UUID, MandateOptionResponse] = {
        option.id: MandateOptionResponse(
            id=str(option.id),
            category_id=str(option.category_id),
            parent_option_id=str(option.parent_option_id) if option.parent_option_id else None,
            option=option.option,
            sub_options=[],
        )
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


@router.get("/categories", response_model=list[MandateCategoryResponse])
async def list_categories(
    claims: dict[str, Any] = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_admin_db),
) -> list[MandateCategoryResponse]:
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


@router.post("/categories", response_model=MandateCategoryResponse, status_code=201)
async def create_category(
    payload: CreateMandateCategoryRequest,
    claims: dict[str, Any] = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_admin_db),
) -> MandateCategoryResponse:
    category = await MandateCategoryRepo(db).create(
        {"category": payload.category, "slug": payload.slug}
    )
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="A mandate category with this name or slug already exists",
        ) from exc

    org_id, actor_id, actor_email = await _admin_actor(db, claims)
    await HumanAuditRepo(db).append(
        {
            "org_id": org_id,
            "actor_id": actor_id,
            "actor_email": actor_email,
            "event_type": "admin_mandate_category_created",
            "payload": {
                "category_id": str(category.id),
                "category": category.category,
                "slug": category.slug,
            },
        }
    )
    return MandateCategoryResponse(
        id=str(category.id), category=category.category, slug=category.slug, options=[]
    )


@router.patch("/categories/{category_id}", response_model=MandateCategoryResponse)
async def update_category(
    category_id: UUID,
    payload: UpdateMandateCategoryRequest,
    claims: dict[str, Any] = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_admin_db),
) -> MandateCategoryResponse:
    category = await MandateCategoryRepo(db).update(category_id, {"category": payload.category})
    if category is None:
        raise HTTPException(status_code=404, detail="Mandate category not found")
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="A mandate category with this name already exists"
        ) from exc

    options = await MandateOptionsRepo(db).list_by_category(category_id)
    org_id, actor_id, actor_email = await _admin_actor(db, claims)
    await HumanAuditRepo(db).append(
        {
            "org_id": org_id,
            "actor_id": actor_id,
            "actor_email": actor_email,
            "event_type": "admin_mandate_category_updated",
            "payload": {"category_id": str(category.id), "category": category.category},
        }
    )
    return MandateCategoryResponse(
        id=str(category.id),
        category=category.category,
        slug=category.slug,
        options=_nest_options_by_category(options).get(category_id, []),
    )


@router.delete("/categories/{category_id}", response_model=SuccessResponse)
async def delete_category(
    category_id: UUID,
    claims: dict[str, Any] = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_admin_db),
) -> SuccessResponse:
    """Deletes the category and, via ON DELETE CASCADE, every option under
    it (see MandateOptions.category_id's FK)."""
    deleted = await MandateCategoryRepo(db).delete(category_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Mandate category not found")

    org_id, actor_id, actor_email = await _admin_actor(db, claims)
    await HumanAuditRepo(db).append(
        {
            "org_id": org_id,
            "actor_id": actor_id,
            "actor_email": actor_email,
            "event_type": "admin_mandate_category_deleted",
            "payload": {"category_id": str(category_id)},
        }
    )
    return SuccessResponse(success=True)


@router.post(
    "/categories/{category_id}/options", response_model=MandateOptionResponse, status_code=201
)
async def create_option(
    category_id: UUID,
    payload: CreateMandateOptionRequest,
    claims: dict[str, Any] = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_admin_db),
) -> MandateOptionResponse:
    if await MandateCategoryRepo(db).get_by_id(category_id) is None:
        raise HTTPException(status_code=404, detail="Mandate category not found")

    option = await MandateOptionsRepo(db).create(
        {"category_id": category_id, "option": payload.option}
    )
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="This option already exists under this category"
        ) from exc

    org_id, actor_id, actor_email = await _admin_actor(db, claims)
    await HumanAuditRepo(db).append(
        {
            "org_id": org_id,
            "actor_id": actor_id,
            "actor_email": actor_email,
            "event_type": "admin_mandate_option_created",
            "payload": {"option_id": str(option.id), "category_id": str(category_id)},
        }
    )
    return MandateOptionResponse(
        id=str(option.id), category_id=str(option.category_id), option=option.option
    )


@router.post(
    "/options/{parent_option_id}/suboptions", response_model=MandateOptionResponse, status_code=201
)
async def create_suboption(
    parent_option_id: UUID,
    payload: CreateMandateSubOptionRequest,
    claims: dict[str, Any] = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_admin_db),
) -> MandateOptionResponse:
    """A route, not a parentOptionId body field on create_option: category_id
    is derived from the parent row, so "parent belongs to a different
    category than the path" can't be expressed -- no validation branch to
    write or forget."""
    parent = await MandateOptionsRepo(db).get_by_id(parent_option_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="Mandate option not found")

    option = await MandateOptionsRepo(db).create(
        {
            "category_id": parent.category_id,
            "parent_option_id": parent.id,
            "option": payload.option,
        }
    )
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="This option already exists under this parent"
        ) from exc

    org_id, actor_id, actor_email = await _admin_actor(db, claims)
    await HumanAuditRepo(db).append(
        {
            "org_id": org_id,
            "actor_id": actor_id,
            "actor_email": actor_email,
            "event_type": "admin_mandate_option_created",
            "payload": {
                "option_id": str(option.id),
                "category_id": str(option.category_id),
                "parent_option_id": str(parent.id),
            },
        }
    )
    return MandateOptionResponse(
        id=str(option.id),
        category_id=str(option.category_id),
        parent_option_id=str(parent.id),
        option=option.option,
    )


@router.patch("/options/{option_id}", response_model=MandateOptionResponse)
async def update_option(
    option_id: UUID,
    payload: UpdateMandateOptionRequest,
    claims: dict[str, Any] = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_admin_db),
) -> MandateOptionResponse:
    option = await MandateOptionsRepo(db).update(option_id, {"option": payload.option})
    if option is None:
        raise HTTPException(status_code=404, detail="Mandate option not found")
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="This option already exists under this category"
        ) from exc

    org_id, actor_id, actor_email = await _admin_actor(db, claims)
    await HumanAuditRepo(db).append(
        {
            "org_id": org_id,
            "actor_id": actor_id,
            "actor_email": actor_email,
            "event_type": "admin_mandate_option_updated",
            "payload": {"option_id": str(option.id)},
        }
    )
    return MandateOptionResponse(
        id=str(option.id),
        category_id=str(option.category_id),
        parent_option_id=str(option.parent_option_id) if option.parent_option_id else None,
        option=option.option,
    )


@router.delete("/options/{option_id}", response_model=SuccessResponse)
async def delete_option(
    option_id: UUID,
    claims: dict[str, Any] = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_admin_db),
) -> SuccessResponse:
    deleted = await MandateOptionsRepo(db).delete(option_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Mandate option not found")

    org_id, actor_id, actor_email = await _admin_actor(db, claims)
    await HumanAuditRepo(db).append(
        {
            "org_id": org_id,
            "actor_id": actor_id,
            "actor_email": actor_email,
            "event_type": "admin_mandate_option_deleted",
            "payload": {"option_id": str(option_id)},
        }
    )
    return SuccessResponse(success=True)
