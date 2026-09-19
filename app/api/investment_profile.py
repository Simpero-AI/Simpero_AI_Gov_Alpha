from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_claims, get_db
from app.models.investment_profile import InvestmentProfile
from app.repo.HumanAuditRepo import HumanAuditRepo
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


@router.put("", response_model=InvestmentProfileResponse)
async def upsert_investment_profile(
    body: UpsertInvestmentProfileRequest,
    claims: dict[str, Any] = Depends(get_claims),
    db: AsyncSession = Depends(get_db),
) -> InvestmentProfileResponse:
    """Create-or-update the org's investment profile. Replaces the retired tRPC
    investmentProfile.upsert (no FastAPI route existed, so the Firm Profile and
    Scoring Framework editors' saves hit an HTML fallback the client couldn't
    parse as JSON). One row per org (unique org_id).

    The body is a PARTIAL patch: FirmProfileBlock saves firm_name + mandate,
    EditableFrameworkBlock saves weights, and they run independently. Any field
    left unset (None) is merged from the stored row so one editor's save never
    blanks the other's slice; a field sent explicitly (including "" or {})
    overwrites. Returns the saved profile as JSON."""
    repo = InvestmentProfileRepo(db)
    user = await UserRepo(db).get_by_clerk_id(claims["user_id"])
    assert user is not None  # get_db JIT-provisions this row before the handler runs

    existing = await repo.get_for_org()

    def _merge(field: str, incoming: Any) -> Any:
        # None == "not sent" -> keep the stored value; anything else overwrites.
        if incoming is not None:
            return incoming
        return getattr(existing, field) if existing is not None else None

    saved = await repo.upsert(
        {
            "org_id": user.org_id,
            "firm_name": _merge("firm_name", body.firm_name),
            "firm_type": _merge("firm_type", body.firm_type),
            "aum_band": _merge("aum_band", body.aum_band),
            "mandate": _merge("mandate", body.mandate),
            "weights": _merge("weights", body.weights),
        }
    )
    await HumanAuditRepo(db).append(
        {
            "org_id": user.org_id,
            "actor_id": claims["user_id"],
            "actor_email": user.email,
            "event_type": "investment_profile_saved",
            "payload": {
                "fields": [
                    name
                    for name, value in (
                        ("firmName", body.firm_name),
                        ("firmType", body.firm_type),
                        ("aumBand", body.aum_band),
                        ("mandate", body.mandate),
                        ("weights", body.weights),
                    )
                    if value is not None
                ]
            },
        }
    )
    return _to_response(saved)
