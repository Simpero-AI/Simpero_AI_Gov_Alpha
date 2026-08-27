"""Enqueue-side client for the standalone parser service (Simpero_Gov_AI_Services).

That service moved out of this repo (formerly services/parser + tests/parser)
and now runs its own SAQ worker against a queue named "parse" on the SAME
Valkey instance this app uses for its own jobs (app/jobs/queue.py). It is a
SEPARATE Queue instance/name — not app.jobs.queue.get_queue()'s "simpero"
queue — because that queue's `functions` list only knows about this app's own
jobs (app/jobs/tasks/__init__.py); enqueuing a parse job there would land in
a queue no worker is listening to.

This module only enqueues and checks status — it does not fetch the result
body itself. The worker writes results to Spaces (see
Simpero_Gov_AI_Services' results_store.py / ParserSettings.results_key_prefix)
and returns a `{bucket, key}` pointer through the queue, never the payload
inline — get_parse_job's `result` dict is that pointer. The claims envelope
`process_document` produces is read back separately, via
app/services/uploads/spaces.py::get_json_object, by whatever ingests it
(app/jobs/tasks/start_deal_verification.py) — not here.
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


async def enqueue_process_document_job(
    spaces_key: str,
    *,
    entity: str,
    known_sha256s: list[str] | None = None,
    sector_options: list[str] | None = None,
    geo_options: list[str] | None = None,
    screen_criteria: list[dict] | None = None,
) -> str:
    """Enqueue the combined parse+extract+audit job for the document already
    uploaded to Spaces at `spaces_key`. Returns the SAQ job key for status
    polling via get_parse_job (saq.Job has no separate "id" — `key` is the
    unique identifier SAQ itself uses, e.g. in Queue.job(key)).

    Named "process_document" (not "parse_document") because it does more:
    docs/plans/analysis-pipeline-stage-chaining.md's point 4 — parsing and
    claim extraction re-enter the same SHA-256-cached parse
    (Simpero_Gov_AI_Services' docling_parser.py), so a second, separate
    parse-only job bought nothing but an extra queue round trip. `audit`
    is not a parameter here: the binding audit is always on, folded into
    this single call, never a caller-supplied option (same doc, point 1).

    `entity` is the claim-attribution label (this app passes `Deal.name`,
    see start_deal_analysis.py) — required by extract_claims on the other
    side, no default.

    `sector_options` / `geo_options` are the org's approved mandate options
    (post sub-tree expansion, from load_workspace_config) for Path B mandate-fit
    classification: the parser judges the target's sector/HQ against these exact
    lists so the backend can write a deal.sector/hq_geography that fold-matches
    gs_08/gs_07. `None` (org has no mandate policy for that dimension) skips the
    fit and the parser reports the raw sector/HQ only.

    `screen_criteria` are the SELECTED qualitative (llm) rules to search the
    document for -- a list of {"rule_id", "question"} (Path B "search just in
    case"). The parser returns a grounded Y/N/unknown per rule, which the backend
    persists and its document evaluators surface. `None`/empty skips the search.

    Kwarg names here must match process_document's parameters exactly — SAQ
    dispatches by keyword.

    Uploading the document to Spaces first is the caller's responsibility —
    this function intentionally does no upload of its own.
    """
    job = await get_parse_queue().enqueue(
        "process_document",
        spaces_key=spaces_key,
        entity=entity,
        known_sha256s=known_sha256s,
        audit=True,
        sector_options=sector_options,
        geo_options=geo_options,
        screen_criteria=screen_criteria,
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
