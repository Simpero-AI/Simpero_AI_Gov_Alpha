from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthenticationError
from app.core.intake_security import IntakeSessionClaims, decode_intake_session_jwt
from app.core.public_dependencies import get_public_session_db
from app.jobs.queue import get_queue
from app.models.deal_intake_link import DealIntakeLink
from app.models.organisation import Organisation
from app.repo.DataSourceRepo import DataSourceRepo
from app.repo.HumanAuditRepo import HumanAuditRepo
from app.schemas.public_uploads import PublicCompleteRequest, PublicPresignRequest
from app.schemas.uploads import CompleteResponse, PresignResponse
from app.services.uploads.spaces import build_object_key, head_object, presign_put

router = APIRouter(prefix="/public/intake/uploads", tags=["public-uploads"])

# Deliberately a separate pair of routes from /api/uploads/*, not a second
# auth branch on the existing ones (P3-10 spec). Constants/validator
# duplicated from app/api/uploads.py rather than shared -- a ~10-line pure
# function isn't worth a module just to avoid one duplication.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".pptx"}
_PRESIGN_TTL_SECONDS = 600

# The real per-link ceiling. /presigned-url's own check is a UX courtesy only
# -- DataSourceRepo.try_create_for_intake_link's advisory-locked count at
# /complete is what actually enforces this.
MAX_FILES_PER_LINK = 20


def _reject_if_bad_type_or_size(filename: str, size: int) -> None:
    if size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"File exceeds the {MAX_UPLOAD_BYTES} byte (10 MB) upload limit",
        )
    suffix = filename[filename.rfind(".") :].lower() if "." in filename else ""
    if not suffix or suffix not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unsupported file type: {filename!r}",
        )


async def _org_name_for_link(db: AsyncSession, link: DealIntakeLink) -> str:
    """Only `name` -- link.org_id (already readable, full-table SELECT grant
    on deal_intake_link) covers the FK value; dd_public's grant on
    organisation is column-restricted to (id, name, clerk_org_id), so this
    stays a scoped select rather than select(Organisation)."""
    name = await db.scalar(
        select(Organisation.name).where(Organisation.clerk_org_id == link.clerk_org_id)
    )
    assert name is not None  # get_public_session_db already vouched for this clerk_org_id
    return name


async def _decode_claims(session_token: str) -> IntakeSessionClaims:
    """Re-decodes the same session token get_public_session_db already
    verified, to reach claims.email -- that dependency's yielded shape
    (AsyncSession, DealIntakeLink) is a pinned contract shared by every other
    P3 public route and its own tests, so it isn't changed here. The decode
    itself is a cheap local HS256 verify, no network call. AuthenticationError
    is caught the same way get_public_session_db catches it -- letting it
    propagate would surface a distinguishable 401 instead of the uniform 404
    every other public-route failure mode returns.
    """
    try:
        return decode_intake_session_jwt(session_token)
    except AuthenticationError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc


@router.post("/presigned-url", response_model=PresignResponse)
async def create_presigned_url(
    body: PublicPresignRequest,
    session_and_link: tuple[AsyncSession, DealIntakeLink] = Depends(get_public_session_db),
) -> PresignResponse:
    db, link = session_and_link
    _reject_if_bad_type_or_size(body.filename, body.size)

    dedupe_candidate = await DataSourceRepo(db).find_dedupe_candidate(
        link.deal_id, body.declared_sha256
    )
    if dedupe_candidate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "A matching file has already been uploaded for this deal",
                "dataSourceId": str(dedupe_candidate.id),
                "status": dedupe_candidate.status,
            },
        )

    # The real 20-file ceiling before a presigned URL is ever issued.
    count = await DataSourceRepo(db).count_for_intake_link(link.id)
    if count >= MAX_FILES_PER_LINK:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This link has already reached the {MAX_FILES_PER_LINK}-file limit",
        )

    upload_id = uuid4()
    org_name = await _org_name_for_link(db, link)

    storage_key = build_object_key(
        org_name, link.clerk_org_id, link.deal_id, upload_id, body.filename
    )
    presigned_url = presign_put(storage_key, ttl_seconds=_PRESIGN_TTL_SECONDS)

    return PresignResponse(
        upload_id=upload_id, presigned_url=presigned_url, storage_key=storage_key
    )


@router.post("/{upload_id}/complete", response_model=CompleteResponse)
async def complete_upload(
    upload_id: UUID,
    body: PublicCompleteRequest,
    session_and_link: tuple[AsyncSession, DealIntakeLink] = Depends(get_public_session_db),
    claims: IntakeSessionClaims = Depends(_decode_claims),
) -> CompleteResponse:
    db, link = session_and_link
    org_name = await _org_name_for_link(db, link)

    storage_key = build_object_key(
        org_name, link.clerk_org_id, link.deal_id, upload_id, body.filename
    )

    if not head_object(storage_key):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Uploaded object not found -- the presigned PUT may not have completed",
        )

    # The real ceiling enforcement (advisory-locked) -- /presigned-url's own
    # check above is a courtesy only and can't prevent a race between two
    # concurrent /complete calls for the same link.
    data_source = await DataSourceRepo(db).try_create_for_intake_link(
        link.id,
        {
            "id": upload_id,
            "org_id": link.org_id,
            "deal_id": link.deal_id,
            "storage_key": storage_key,
            "filename": body.filename,
            "declared_sha256": body.declared_sha256,
            "status": "pending",
        },
        ceiling=MAX_FILES_PER_LINK,
    )
    if data_source is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This link has already reached the {MAX_FILES_PER_LINK}-file limit",
        )

    # Same queue/timeout/retries as the authenticated path -- the ticket's
    # "byte-for-byte identical data_source row" implies the same downstream
    # ingest processing too.
    await get_queue().enqueue(
        "ingest_data_source",
        data_source_id=str(upload_id),
        clerk_org_id=link.clerk_org_id,
        storage_key=storage_key,
        declared_sha256=body.declared_sha256,
        timeout=120,
        retries=2,
    )

    await HumanAuditRepo(db).append(
        {
            "org_id": link.org_id,
            "actor_id": None,
            "actor_email": claims.email,
            "event_type": "intake_document_uploaded",
            "deal_id": link.deal_id,
            "payload": {
                "data_source_id": str(upload_id),
                "filename": body.filename,
                "storage_key": storage_key,
            },
        }
    )

    return CompleteResponse(id=data_source.id, status=data_source.status)
