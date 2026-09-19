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
    """investmentProfile.upsert body — partial by design: only the fields the
    caller sends are written, so Firm Profile (firm_name + mandate) and Scoring
    Framework (weights) each save their own slice without clobbering the
    other's column. Every field is optional and defaults unset (never sent =>
    excluded from model_dump(exclude_unset=True) => that column is left
    untouched). Each JSONB field is a full replace of that column, so the
    caller merges its slice into the existing blob client-side before sending.
    """

    firm_name: str | None = None
    firm_type: str | None = None
    aum_band: str | None = None
    mandate: dict[str, Any] | None = None
    weights: dict[str, Any] | None = None
