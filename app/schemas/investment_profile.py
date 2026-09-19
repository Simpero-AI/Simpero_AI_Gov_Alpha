from datetime import datetime
from typing import Any

from app.schemas.common import CamelModel


class InvestmentProfileResponse(CamelModel):
    firm_name: str | None
    firm_type: str | None
    aum_band: str | None
    mandate: dict[str, Any]
    weights: dict[str, Any]
    updated_at: datetime


class UpsertInvestmentProfileRequest(CamelModel):
    """PUT /investment-profile body. Every field is OPTIONAL and defaults to
    None so the two independent editors on /mandate-scorecard can each save the
    slice they own without clobbering the other's: FirmProfileBlock sends
    firm_name + mandate, EditableFrameworkBlock sends weights. A field left unset
    (None) keeps the stored value; a field sent explicitly (including an empty
    string or {}) overwrites it. Replaces the retired tRPC
    investmentProfile.upsert, which had no FastAPI route and 404'd."""

    firm_name: str | None = None
    firm_type: str | None = None
    aum_band: str | None = None
    mandate: dict[str, Any] | None = None
    weights: dict[str, Any] | None = None
