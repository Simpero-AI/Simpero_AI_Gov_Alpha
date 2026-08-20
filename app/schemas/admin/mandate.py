from enum import StrEnum

from pydantic import model_validator

from app.schemas.common import CamelModel


class MandateCategorySlug(StrEnum):
    """The fixed, backend-owned set of category identities the product
    Builder renders UI for (Simpero_AI_Gov_Web's mandateSelection.ts) --
    the only slugs a category can ever be created with. Values double as
    the canonical display name lookup in CANONICAL_LABELS below."""

    INVESTMENT_STAGE = "investment_stage"
    GEOGRAPHIES = "geographies"
    TARGET_SECTORS = "target_sectors"
    DEAL_TYPES = "deal_types"
    ASSET_CLASSES = "asset_classes"
    MUST_HAVE = "must_have"
    DEAL_BREAKER = "deal_breaker"
    CHECK_SIZE_RANGE = "check_size_range"


CANONICAL_LABELS: dict[MandateCategorySlug, str] = {
    MandateCategorySlug.INVESTMENT_STAGE: "Investment Stage",
    MandateCategorySlug.GEOGRAPHIES: "Geographies",
    MandateCategorySlug.TARGET_SECTORS: "Target Sectors",
    MandateCategorySlug.DEAL_TYPES: "Deal Types",
    MandateCategorySlug.ASSET_CLASSES: "Asset Classes",
    MandateCategorySlug.MUST_HAVE: "Must Have",
    MandateCategorySlug.DEAL_BREAKER: "Deal Breaker",
    MandateCategorySlug.CHECK_SIZE_RANGE: "Check Size Range",
}


class MandateOptionResponse(CamelModel):
    id: str
    category_id: str
    parent_option_id: str | None = None
    option: str
    sub_options: list["MandateOptionResponse"] = []


MandateOptionResponse.model_rebuild()


class MandateCategoryResponse(CamelModel):
    id: str
    category: str
    slug: str | None = None
    options: list[MandateOptionResponse]


class CreateMandateCategoryRequest(CamelModel):
    """`category` defaults to the slug's canonical label but stays editable
    from the start -- set explicitly to override it at creation time.
    `slug` is optional: omitting it (None) creates a category outside the
    fixed eight, invisible to the product Builder, same as today."""

    category: str | None = None
    slug: MandateCategorySlug | None = None

    @model_validator(mode="after")
    def _default_category_from_slug(self) -> "CreateMandateCategoryRequest":
        if self.category is None:
            if self.slug is None:
                raise ValueError("category is required when slug is not provided")
            self.category = CANONICAL_LABELS[self.slug]
        return self


class UpdateMandateCategoryRequest(CamelModel):
    """Display name only -- slug is immutable for the row's lifetime and
    has no place in this request."""

    category: str


class CreateMandateOptionRequest(CamelModel):
    option: str


class UpdateMandateOptionRequest(CamelModel):
    option: str


class CreateMandateSubOptionRequest(CamelModel):
    """Deliberately identical in shape to CreateMandateOptionRequest but kept
    as its own class -- the parent comes from the path, and reusing the
    class would invite adding a parent_option_id body field later, which the
    endpoint design (POST /options/{parent_option_id}/suboptions) rules out."""

    option: str
