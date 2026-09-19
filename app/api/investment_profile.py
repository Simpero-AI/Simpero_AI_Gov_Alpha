from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_claims, get_db
from app.models.investment_profile import InvestmentProfile
from app.repo.InvestmentProfileRepo import InvestmentProfileRepo
from app.repo.UserRepo import UserRepo
from app.schemas.investment_profile import (
    InvestmentProfileResponse,
    UpsertInvestmentProfileRequest,
)

router = APIRouter(prefix="/investment-profile", tags=["investment-profile"])


def _to_response(profile: InvestmentProfile) -> InvestmentProfileResponse:
    return InvestmentProfileResponse(
        firm_name=profile.firm_name,
        firm_type=profile.firm_type,
        aum_band=profile.aum_band,
        mandate=profile.mandate or {},
        weights=profile.weights or {},
        updated_at=profile.updated_at,
    )


@router.get("", response_model=InvestmentProfileResponse | None)
async def get_investment_profile(
    db: AsyncSession = Depends(get_db),
) -> InvestmentProfileResponse | None:
    """investmentProfile.get — null (never 404) when the org has no profile
    row yet; keeps the UI's empty states."""
    profile = await InvestmentProfileRepo(db).get_for_org()
    if profile is None:
        return None
    return _to_response(profile)


@router.api_route("", methods=["POST", "PUT"], response_model=InvestmentProfileResponse)
async def upsert_investment_profile(
    body: UpsertInvestmentProfileRequest,
    claims: dict[str, Any] = Depends(get_claims),
    db: AsyncSession = Depends(get_db),
) -> InvestmentProfileResponse:
    """investmentProfile.upsert — create-or-replace the org's firm profile.
    Partial by design (see UpsertInvestmentProfileRequest): Firm Profile saves
    firm_name + mandate, Scoring Framework saves weights, each without
    clobbering the other's column. One row per org (unique org_id); org
    isolation is enforced by RLS (get_db's SET LOCAL app.org_id), same as the
    GET above and the sibling PUT /mandate."""
    user = await UserRepo(db).get_by_clerk_id(claims["user_id"])
    assert user is not None  # get_db JIT-provisions this row before the handler runs
    # Drop None-valued keys, not just unset ones: a client that serializes an
    # untouched field as explicit null (e.g. the Scoring Framework tab sending
    # weights + firmName: null) must not clobber the other tab's column. A real
    # clear uses "" for a text field, never null, so nothing legitimate is lost.
    data = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    profile = await InvestmentProfileRepo(db).upsert_for_org(user.org_id, data)
    return _to_response(profile)
