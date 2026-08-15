from app.schemas.common import CamelModel


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
    options: list[MandateOptionResponse]


class CreateMandateCategoryRequest(CamelModel):
    category: str


class UpdateMandateCategoryRequest(CamelModel):
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
