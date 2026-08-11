"""Enqueue-side client for the standalone parser service (Simpero_Gov_AI_Services).

That service moved out of this repo (formerly services/parser + tests/parser)
and now runs its own SAQ worker against a queue named "parse" on the SAME
Valkey instance this app uses for its own jobs (app/jobs/queue.py). It is a
SEPARATE Queue instance/name — not app.jobs.queue.get_queue()'s "simpero"
queue — because that queue's `functions` list only knows about this app's own
jobs (app/jobs/tasks/__init__.py); enqueuing a parse job there would land in
a queue no worker is listening to.

This module only enqueues and checks status — it does not fetch the parsed
result. The worker writes results to Spaces (see Simpero_Gov_AI_Services'
results_store.py / ParserSettings.results_key_prefix); reading them back
requires boto3, which this app already depends on (pyproject.toml — kept for
future Spaces access from this app; app/services/uploads/spaces.py already
reuses the parser service's bucket). Add that read once something here
actually needs to open a parsed result rather than just trigger and poll a
job — get_parse_job's `result` dict is a bucket+key pointer, not the body.
"""

from functools import lru_cache

from saq import Queue
from saq.job import Job

from app.core.config import get_settings

# Must match Simpero_Gov_AI_Services' ParserSettings.queue_name exactly — a
# mismatch here means jobs are enqueued and never picked up, with no error on
# either side. Documented in both repos' CLAUDE.md/README as the shared
# contract; there is no code-level guard against it drifting.
PARSE_QUEUE_NAME = "parse"


@lru_cache
def get_parse_queue() -> Queue:
    # Lazy, same reasoning as app/jobs/queue.py::get_queue — no connection at
    # import time.
    settings = get_settings()
    return Queue.from_url(settings.valkey_url, name=PARSE_QUEUE_NAME)


async def enqueue_parse_job(spaces_key: str, known_sha256s: list[str] | None = None) -> str:
    """Enqueue a parse job for the document already uploaded to Spaces at
    `spaces_key`. Returns the SAQ job key for status polling via get_parse_job
    (saq.Job has no separate "id" — `key` is the unique identifier SAQ itself
    uses, e.g. in Queue.job(key)).

    Uploading the document to Spaces first is the caller's responsibility —
    there is no data-source/upload model in this app yet (app/api/deals.py is
    still a stub), so this function intentionally does no upload of its own.
    """
    job = await get_parse_queue().enqueue(
        "parse_document",
        spaces_key=spaces_key,
        known_sha256s=known_sha256s,
    )
    assert job is not None, (
        "enqueue() only returns None if the job already exists uniquely and was skipped"
    )
    return job.key


async def get_parse_job(job_key: str) -> Job | None:
    """Fetch current status/result for a previously enqueued parse job.

    Returns None if the job key is unknown (already expired from Valkey, or
    never existed) — callers should treat that as "not found", not as an
    in-progress state.
    """
    return await get_parse_queue().job(job_key)
