from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_claims, get_db
from app.jobs.queue import get_queue
from app.models.organisation import Organisation
from app.repo.DataSourceRepo import DataSourceRepo
from app.repo.HumanAuditRepo import HumanAuditRepo
from app.repo.UserRepo import UserRepo
from app.schemas.uploads import CompleteRequest, CompleteResponse, PresignRequest, PresignResponse
from app.services.uploads.spaces import build_object_key, head_object, presign_put

router = APIRouter(prefix="/uploads", tags=["uploads"])

# Decided by Vansh (see docs/plans/document-upload-presigned-urls.md, Storage
# / Spaces configuration): 10 MB flat, no multiplier. This is a declared-size
# guard only -- nothing has been uploaded yet at this point; the ingest job
# (Phase 5) is the real enforcement backstop against the measured bytes.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# Not specified verbatim by the plan beyond "file type reject" -- a reasonable
# implementer default covering the document types this app already deals with
# (deal decks/financials, per existing seeded test fixtures using .pdf/.xlsx).
# Flag to Vansh if a different whitelist is actually wanted.
_ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".pptx"}

# Presigned PUT TTL -- 10 minutes, decided by Vansh (Request/response contract
# section is authoritative over a stale "Risks" bullet elsewhere in the plan).
_PRESIGN_TTL_SECONDS = 600


async def _org_for_request(db: AsyncSession, claims: dict[str, Any]) -> Organisation:
    org = await db.scalar(
        select(Organisation).where(Organisation.clerk_org_id == claims["tenant_id"])
    )
    assert org is not None  # get_db JIT-provisions this row before the handler runs
    return org


async def _actor(db: AsyncSession, claims: dict[str, Any]) -> tuple[int, str, str | None]:
    """(org_id, actor_id, actor_email) for the audit rows this router appends
    -- same pattern as app/api/deals.py::_actor."""
    user = await UserRepo(db).get_by_clerk_id(claims["user_id"])
    assert user is not None  # get_db JIT-provisions this row before the handler runs
    return user.org_id, claims["user_id"], user.email


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


@router.post("/presigned-url", response_model=PresignResponse)
async def create_presigned_url(
    body: PresignRequest,
    claims: dict[str, Any] = Depends(get_claims),
    db: AsyncSession = Depends(get_db),
) -> PresignResponse:
    """SIM-220. Control-plane only -- the client PUTs directly to Spaces with
    the returned URL, never through this app. No DB write here: the
    data_source row is created at /complete (Phase 4) once the PUT is
    confirmed via head_object.
    """
    _reject_if_bad_type_or_size(body.filename, body.size)

    # Dedupe, scoped to declared_sha256 OR fingerprint (not fingerprint
    # alone) so a duplicate upload started before a prior upload's ingest job
    # finishes is still caught -- see DataSourceRepo.find_dedupe_candidate.
    dedupe_candidate = await DataSourceRepo(db).find_dedupe_candidate(
        body.deal_id, body.declared_sha256
    )
    if dedupe_candidate is not None:
        # Structured, not a bare string: the frontend's upload pipeline
        # (Simpero_AI_Gov_Web's documentUploadPipeline.ts) treats this as
        # equivalent to a fresh successful upload -- the file is already
        # accounted for -- and needs the existing row's id/status to do so
        # without fabricating data.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "A matching file has already been uploaded for this deal",
                "dataSourceId": str(dedupe_candidate.id),
                "status": dedupe_candidate.status,
            },
        )

    upload_id = uuid4()

    org = await _org_for_request(db, claims)

    storage_key = build_object_key(
        org.name, claims["tenant_id"], body.deal_id, upload_id, body.filename
    )
    presigned_url = presign_put(storage_key, ttl_seconds=_PRESIGN_TTL_SECONDS)

    return PresignResponse(
        upload_id=upload_id, presigned_url=presigned_url, storage_key=storage_key
    )


@router.post("/{upload_id}/complete", response_model=CompleteResponse)
async def complete_upload(
    upload_id: UUID,
    body: CompleteRequest,
    claims: dict[str, Any] = Depends(get_claims),
    db: AsyncSession = Depends(get_db),
) -> CompleteResponse:
    """SIM-216. Confirms the presigned PUT actually happened, registers the
    data_source row, and enqueues the async ingest job (Phase 5). storage_key
    is never accepted from the client -- it's re-derived server-side the same
    deterministic way /presigned-url derived it, then checked against Spaces
    directly via head_object before anything is written.
    """
    org = await _org_for_request(db, claims)

    storage_key = build_object_key(
        org.name, claims["tenant_id"], body.deal_id, upload_id, body.filename
    )

    # Confirms the PUT actually happened -- without this, a client could call
    # /complete having never uploaded, creating a phantom pending row that can
    # never be deleted (REVOKE DELETE) and would sit forever failing ingest.
    if not head_object(storage_key):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Uploaded object not found -- the presigned PUT may not have completed",
        )

    data_source = await DataSourceRepo(db).create(
        {
            "id": upload_id,
            "org_id": org.id,
            "deal_id": body.deal_id,
            "storage_key": storage_key,
            "filename": body.filename,
            "declared_sha256": body.declared_sha256,
            "status": "pending",
        }
    )

    # "simpero" queue -- this app's own SAQ worker. Never get_parse_queue()'s
    # "parse" queue, which targets a different service's worker that doesn't
    # consume this job name; getting this backwards silently drops the job.
    # Explicit timeout: SAQ's default is 10s, and stream_and_hash's round trip
    # to Spaces for a real document routinely runs longer than that -- a job
    # cancelled by SAQ mid-UPDATE rolls back, leaving the row stuck "pending"
    # with no automatic retry (default retries=1 doesn't cover a timeout kill).
    await get_queue().enqueue(
        "ingest_data_source",
        data_source_id=str(upload_id),
        clerk_org_id=claims["tenant_id"],
        storage_key=storage_key,
        declared_sha256=body.declared_sha256,
        timeout=120,
        retries=2,
    )

    org_id, actor_id, actor_email = await _actor(db, claims)
    await HumanAuditRepo(db).append(
        {
            "org_id": org_id,
            "actor_id": actor_id,
            "actor_email": actor_email,
            "event_type": "document_upload_completed",
            "deal_id": body.deal_id,
            "payload": {
                "data_source_id": str(upload_id),
                "filename": body.filename,
                "storage_key": storage_key,
            },
        }
    )

    return CompleteResponse(id=data_source.id, status=data_source.status)
