from uuid import UUID

from app.schemas.common import CamelModel


class PresignRequest(CamelModel):
    """POST /uploads/presigned-url body. `size`/`declared_sha256` are
    client-declared, not measured -- nothing has been uploaded yet at this
    point (see the type/size guard in app/api/uploads.py)."""

    deal_id: UUID
    filename: str
    size: int
    declared_sha256: str


class PresignResponse(CamelModel):
    """POST /uploads/presigned-url response. No DB row is created here --
    that happens at /complete (Phase 4)."""

    upload_id: UUID
    presigned_url: str
    storage_key: str


class CompleteRequest(CamelModel):
    """POST /uploads/{upload_id}/complete body. The client re-sends exactly
    what it already holds from its own /presigned-url call -- no new
    client-side state, no storage_key (that's server-recomputed, never
    trusted from the client -- see app/api/uploads.py)."""

    deal_id: UUID
    filename: str
    declared_sha256: str


class CompleteResponse(CamelModel):
    """POST /uploads/{upload_id}/complete response -- the created row's
    id/status, plus a best-effort page count for the frontend's upload-time
    page-cap check. None when the file isn't a PDF or its page count
    couldn't be determined -- never fails the request."""

    id: UUID
    status: str
    page_count: int | None = None
