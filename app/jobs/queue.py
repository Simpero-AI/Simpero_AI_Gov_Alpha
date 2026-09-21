from functools import lru_cache

from saq import Queue

from app.core.config import get_settings

settings = get_settings()


@lru_cache
def get_queue() -> Queue:
    # Queue.from_url is lazy — it does not open a connection until the first command is issued,
    # so constructing it here does not violate the no-connections-at-startup rule.
    return Queue.from_url(settings.valkey_url, name="simpero")


async def enqueue_ingest_data_source(
    *,
    data_source_id: str,
    clerk_org_id: str,
    storage_key: str,
    declared_sha256: str,
) -> None:
    """Enqueue the async document-ingest job for an already-uploaded object.

    MUST be scheduled to run only AFTER the data_source row's request
    transaction has committed -- the job looks the row up by id under the org's
    RLS and aborts ("data_source ... not found") if it is not yet durable. The
    upload handlers therefore hand this to a FastAPI BackgroundTask (which runs
    after the response, i.e. after get_db commits, and is skipped when the commit
    raises) rather than awaiting it inside the request transaction, where a
    fast worker could dequeue before the commit (transient miss) OR a later
    rollback could orphan the job against a row that never persisted (permanent
    miss). See docs/plans / the analysis-job chain, which enqueues post-commit
    for exactly this reason.

    On the "simpero" queue -- this app's own SAQ worker, never the parser's
    'parse' queue (a different service that doesn't consume this job name).
    Explicit timeout=120: stream_and_hash's Spaces round trip for a real
    document routinely exceeds SAQ's 10s default; retries=2 covers a transient
    dequeue-before-commit lag.
    """
    await get_queue().enqueue(
        "ingest_data_source",
        data_source_id=data_source_id,
        clerk_org_id=clerk_org_id,
        storage_key=storage_key,
        declared_sha256=declared_sha256,
        timeout=120,
        retries=2,
    )
