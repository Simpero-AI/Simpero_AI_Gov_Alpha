"""Async ingest job (SIM-216/218 Phase 5): verifies an uploaded object's real
bytes against its client-declared hash and finalizes the data_source row's
terminal status.

Runs in the SAQ worker process -- there is no FastAPI request/Depends(get_db)
here, so this replicates get_db's `SET LOCAL app.org_id` discipline by hand
(see app/core/dependencies.py::get_db's docstring for why SET LOCAL, not SET
or SET SESSION, is mandatory under PgBouncer transaction pooling: the GUC
must be transaction-scoped so it never leaks to the next tenant PgBouncer
hands the reclaimed backend connection to).
"""

import logging
from uuid import UUID

from saq.types import Context
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.repo.DataSourceRepo import DataSourceRepo
from app.repo.HumanAuditRepo import HumanAuditRepo
from app.services.uploads.spaces import ObjectTooLargeError, stream_and_hash

logger = logging.getLogger(__name__)

# Same 10 MB ceiling as the declared-size guard at /presigned-url (Phase 3) --
# this is the real enforcement backstop against measured bytes, the declared
# guard only checks a client-supplied value nothing has verified yet.
_MAX_INGEST_BYTES = 10 * 1024 * 1024


async def ingest_data_source(
    ctx: Context,
    *,
    data_source_id: str,
    clerk_org_id: str,
    storage_key: str,
    declared_sha256: str,
) -> None:
    async with AsyncSessionLocal() as session, session.begin():
        # SET LOCAL must be the first statement in this transaction -- see
        # module docstring. set_config(..., true) IS "SET LOCAL" in function
        # form; a bare `SET LOCAL x = :p` can't bind parameters.
        await session.execute(
            text("SELECT set_config('app.org_id', :tid, true)"),
            {"tid": clerk_org_id},
        )

        repo = DataSourceRepo(session)
        row_id = UUID(data_source_id)
        data_source = await repo.get_by_id(row_id)
        if data_source is None:
            # /complete's enqueue can land in Valkey slightly before its own
            # DB transaction commits -- a very fast worker could look this up
            # before the row is visible. Raise rather than silently no-op so
            # SAQ's own retry mechanism handles the transient case; no custom
            # retry/backoff is added here.
            logger.warning(
                "ingest: data_source %s not found (storage_key=%s) -- likely the "
                "enqueue-before-commit race; raising for SAQ retry",
                data_source_id,
                storage_key,
            )
            raise ValueError(f"data_source {data_source_id} not found")

        # Idempotency guard: a row that's already left 'pending' is done.
        # Checked before stream_and_hash so a re-delivered/duplicate job never
        # re-downloads or re-hashes an object whose outcome is already
        # terminal -- and never attempts a second UPDATE (the one-way trigger
        # would reject it anyway, but this is a courtesy, not a substitute).
        if data_source.status != "pending":
            logger.debug(
                "ingest: data_source %s already terminal (status=%s); no-op",
                data_source_id,
                data_source.status,
            )
            return

        try:
            fingerprint = stream_and_hash(storage_key, max_bytes=_MAX_INGEST_BYTES)
        except ObjectTooLargeError:
            status = "quarantined"
            fingerprint = None
        else:
            status = "verified" if fingerprint == declared_sha256 else "mismatch"

        # verified -> the pipeline proceeds; mismatch/quarantined -> this document is
        # DROPPED from analysis, so surface it loudly (a silently missing doc is
        # exactly the kind of thing this observability pass exists to expose).
        if status == "verified":
            logger.info(
                "ingest: data_source %s verified (deal=%s)", data_source_id, data_source.deal_id
            )
        else:
            logger.warning(
                "ingest: data_source %s -> %s (deal=%s) -- dropped from analysis; "
                "declared_sha256=%s measured=%s",
                data_source_id,
                status,
                data_source.deal_id,
                declared_sha256,
                fingerprint,
            )

        await repo.update_status(row_id, status=status, fingerprint=fingerprint)
        await HumanAuditRepo(session).append(
            {
                "org_id": data_source.org_id,
                "actor_id": "Internal System",
                "actor_email": "Internal System",
                "event_type": "document_upload_ingest_completed",
                "deal_id": data_source.deal_id,
                "payload": {
                    "data_source_id": data_source_id,
                    "status": status,
                    "fingerprint": fingerprint,
                },
            }
        )
