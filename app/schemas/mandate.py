from datetime import datetime
from typing import Any

from app.schemas.common import CamelModel


class MandateOptionResponse(CamelModel):
    id: str
    option: str
    sub_options: list["MandateOptionResponse"] = []


MandateOptionResponse.model_rebuild()


class MandateCategoryResponse(CamelModel):
    id: str
    category: str
    options: list[MandateOptionResponse]


class MandateResponse(CamelModel):
    mandate: list[Any]
    updated_at: datetime


class UpsertMandateRequest(CamelModel):
    mandate: list[Any]
