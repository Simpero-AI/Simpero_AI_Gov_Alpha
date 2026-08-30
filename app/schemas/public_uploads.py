from pydantic import Field

from app.schemas.common import CamelModel


class PublicPresignRequest(CamelModel):
    """POST /public/intake/uploads/presigned-url body. No deal_id -- unlike
    PresignRequest (app/schemas/uploads.py), the deal is derived entirely
    from the intake session (link.deal_id), never accepted from the client.
    """

    filename: str
    # ge=1: size is bound into presign_put's signature (P3-15/F9) -- a
    # zero/negative value would still pass the upper-bound check in
    # _reject_if_bad_type_or_size and get signed into a URL no real PUT
    # could ever satisfy. Fails closed either way, but rejecting it here is
    # a normal 422 instead of a wasted round trip ending in an opaque 403.
    size: int = Field(ge=1)
    declared_sha256: str


class PublicCompleteRequest(CamelModel):
    """POST /public/intake/uploads/{upload_id}/complete body. Same
    deal-from-session reasoning as PublicPresignRequest above."""

    filename: str
    declared_sha256: str
