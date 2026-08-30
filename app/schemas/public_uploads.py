from app.schemas.common import CamelModel


class PublicPresignRequest(CamelModel):
    """POST /public/intake/uploads/presigned-url body. No deal_id -- unlike
    PresignRequest (app/schemas/uploads.py), the deal is derived entirely
    from the intake session (link.deal_id), never accepted from the client.
    """

    filename: str
    size: int
    declared_sha256: str


class PublicCompleteRequest(CamelModel):
    """POST /public/intake/uploads/{upload_id}/complete body. Same
    deal-from-session reasoning as PublicPresignRequest above."""

    filename: str
    declared_sha256: str
