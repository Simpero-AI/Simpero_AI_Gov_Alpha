"""Start-analysis fan-out job (docs/plans/start-analysis-flow-alpha.md,
reworked per docs/plans/analysis-pipeline-stage-chaining.md's point 4):
resolves an analysis_run, fans its deal's already-verified documents out to
the parser service's "parse" queue (app/jobs/parse_client.py) as combined
parse+extract+audit jobs, and awaits every one to a terminal outcome before
marking the run successful/failed. On success, enqueues the ingest+verify
task (start_deal_verification) -- this job's `job_name` stays `"parsing"`
for its whole lifetime; there is no separate `job_name="extraction"` row,
since extraction now happens inside this same per-document job.

Runs in the SAQ worker process -- there is no FastAPI request/Depends(get_db)
here, so this replicates get_db's `SET LOCAL app.org_id` discipline by hand,
exactly like app/jobs/tasks/ingest_data_source.py (see that module's
docstring for the full PgBouncer transaction-pooling reasoning).

Unlike that task, this one runs for minutes to hours -- the parser's own
per-document timeout is 1800s with 2 retries (D8). So this task never holds
one transaction open across the wait: every read/write below opens its own
short-lived session, commits, and closes *before* the asyncio.sleep, then
re-issues `SET LOCAL app.org_id` from scratch as the first statement of the
next transaction. Holding a transaction open across the wait would pin a
PgBouncer backend connection for the run's entire lifetime, defeating the
pooler.
"""

import asyncio
from uuid import UUID, uuid4

from saq.job import TERMINAL_STATUSES, Status
from saq.types import Context
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.jobs.parse_client import enqueue_process_document_job, get_parse_job
from app.jobs.queue import get_queue
from app.repo.AnalysisRunRepo import AnalysisRunRepo
from app.repo.DataSourceRepo import DataSourceRepo
from app.repo.DealRepo import DealRepo
from app.repo.HumanAuditRepo import HumanAuditRepo

# D8/D9: worst case is N documents x (1800s parser timeout x 2 parser
# retries), serialized on the parser's own concurrency:1 worker. Picked, not
# derived -- see the plan's Open Question 5.
_DEADLINE_SECONDS = 7200
_POLL_INTERVAL_SECONDS = 15


async def _set_org(session, clerk_org_id: str) -> None:
    # set_config(..., true) IS "SET LOCAL" in function form -- a bare
    # `SET LOCAL x = :p` can't bind parameters. Must be the first statement
    # in the transaction; see module docstring.
    await session.execute(
        text("SELECT set_config('app.org_id', :tid, true)"),
        {"tid": clerk_org_id},
    )


async def _apply_outcome(ds_repo: DataSourceRepo, job: dict, result: dict) -> dict:
    """Returns a NEW parse_jobs entry with this job's terminal outcome
    applied, and — for the no_extractable_text rejection — writes
    data_source.status (SIM-350, Option A: verified -> ocr_needed is now a
    legal transition).

    Deliberately builds a new dict rather than mutating `job` in place:
    `job` is one element of the list loaded from run.parse_jobs earlier in
    this transaction, still referenced by that ORM attribute. Mutating it
    in place would make the "before" value SQLAlchemy compares against at
    flush time equal to the "after" value (same dict object, already
    changed) -- the UPDATE would be silently skipped, exactly the kind of
    bug JSONB columns need `sqlalchemy.ext.mutable` to avoid otherwise.
    """
    job = {
        **job,
        "outcome": result.get("status"),
        "code": result.get("code"),
        # The parser's own human-readable rejection message (worker.py's
        # ParseError -> {"status": "rejected", "code", "message"}). None on
        # a "parsed" result -- the parser has no equivalent narrative field
        # for success, only bucket/key/kind/sha256/count metadata.
        "message": result.get("message"),
        "bucket": result.get("bucket"),
        "key": result.get("key"),
    }

    if result.get("status") == "rejected" and result.get("code") == "no_extractable_text":
        data_source = await ds_repo.get_by_id(UUID(job["data_source_id"]))
        if data_source is not None and data_source.status == "verified":
            # Implementer trap (see the plan's "Blocking prerequisite"):
            # update_status writes fingerprint unconditionally -- pass the
            # row's existing fingerprint, never None, or this wipes the
            # already-verified hash.
            await ds_repo.update_status(
                data_source.id, status="ocr_needed", fingerprint=data_source.fingerprint
            )

    return job


def _final_status(parse_jobs: list[dict], timed_out: bool) -> tuple[str, str | None]:
    """D14/D15: a run with zero successful parses is `failed`, named why.
    Mixed outcomes (some parsed, some rejected) -> `successful`, not
    `failed`. (`job["outcome"]` is the parser service's own per-document
    vocabulary -- "parsed"/"rejected" -- unrelated to and never renamed
    alongside analysis_run.status's own four values.)"""
    parsed_count = sum(1 for job in parse_jobs if job["outcome"] == "parsed")
    if parsed_count > 0:
        return "successful", None
    if timed_out:
        return "failed", "Analysis timed out waiting for documents to finish parsing."
    rejected = [job for job in parse_jobs if job["outcome"] == "rejected"]
    if rejected and all(job["code"] == "no_extractable_text" for job in rejected):
        noun = "document" if len(rejected) == 1 else f"{len(rejected)} documents"
        return "failed", f"All {noun} need OCR before analysis."
    return "failed", "None of this deal's documents could be parsed."


def _comment_for_job(job: dict, timed_out: bool) -> str:
    """The comment is the parser service's own words wherever it supplied
    any -- `message`, straight from its ParseError, for a rejected outcome.
    This app only invents wording where the parser genuinely has none: a
    "parsed" success (no narrative field on that side, just bucket/key/kind
    metadata) and a SAQ-level job failure (never went through the parser's
    own error path at all, so there's no message to have)."""
    if job["outcome"] == "parsed":
        return "Parsed successfully."
    if job["outcome"] == "rejected":
        message = job.get("message")
        if message:
            return message
        if job["code"] in ("job_failed", "job_aborted"):
            return "Parsing job failed unexpectedly."
        return "Parsing was rejected."
    return "Timed out waiting for the parser." if timed_out else "Still waiting on the parser."


def _build_job_comments(parse_jobs: list[dict], timed_out: bool) -> list[dict]:
    """Frontend-facing findings summary, derived from `parse_jobs` at the
    moment the run goes terminal. Unlike `parse_jobs` (this task's own
    bookkeeping shape -- job_key/bucket/key, snake_case), this is meant to
    be read directly off `GET .../status`, so it's camelCase and carries
    only what a UI needs: which document, what happened, in plain words."""
    return [
        {
            "dataSourceId": job["data_source_id"],
            "fileName": job.get("filename"),
            "status": job["outcome"] or "pending",
            "comment": _comment_for_job(job, timed_out),
        }
        for job in parse_jobs
    ]


async def start_deal_analysis(
    ctx: Context,
    *,
    analysis_run_id: str,
    deal_id: str,
    clerk_org_id: str,
) -> None:
    run_id = UUID(analysis_run_id)

    # Step 1: snapshot usable documents (D13: status == 'verified' only —
    # this read is authoritative, superseding whatever the request handler
    # saw) and enqueue a parse job for each one not already recorded (D11:
    # idempotent, so a SAQ redelivery resumes instead of double-enqueuing).
    async with AsyncSessionLocal() as session, session.begin():
        await _set_org(session, clerk_org_id)
        run_repo = AnalysisRunRepo(session)
        run = await run_repo.get_by_id(run_id)
        if run is None:
            raise ValueError(f"analysis_run {analysis_run_id} not found")

        deal = await DealRepo(session).get_by_id(UUID(deal_id))
        assert deal is not None  # RLS already scoped this read to the run's own org

        data_sources = await DataSourceRepo(session).list_for_deal(UUID(deal_id))
        usable = [ds for ds in data_sources if ds.status == "verified"]

        parse_jobs = list(run.parse_jobs or [])
        already_enqueued = {job["data_source_id"] for job in parse_jobs}
        for data_source in usable:
            if str(data_source.id) in already_enqueued:
                continue
            # D12: known_sha256s is a duplicate-*rejection* list on the
            # parser's side -- never pass the document's own fingerprint.
            job_key = await enqueue_process_document_job(
                data_source.storage_key, entity=deal.name, known_sha256s=None
            )
            parse_jobs.append(
                {
                    "data_source_id": str(data_source.id),
                    "filename": data_source.filename,
                    "storage_key": data_source.storage_key,
                    "job_key": job_key,
                    "outcome": None,
                    "code": None,
                    "message": None,
                    "bucket": None,
                    "key": None,
                }
            )

        await run_repo.update_progress(run_id, status="in_progress", parse_jobs=parse_jobs)
        org_id, deal_uuid = run.org_id, run.deal_id

    # Step 2: poll every recorded job to a terminal SAQ status, persisting
    # outcomes as they land, without holding a transaction open across the
    # wait (D10).
    loop = asyncio.get_event_loop()
    deadline = loop.time() + _DEADLINE_SECONDS
    verification_run_id: UUID | None = None
    while True:
        async with AsyncSessionLocal() as session, session.begin():
            await _set_org(session, clerk_org_id)
            run_repo = AnalysisRunRepo(session)
            ds_repo = DataSourceRepo(session)

            run = await run_repo.get_by_id(run_id)
            if run is None:
                raise ValueError(f"analysis_run {analysis_run_id} not found")

            # Never mutate the dicts inside run.parse_jobs in place -- see
            # _apply_outcome's docstring for why that would make SQLAlchemy
            # silently skip the UPDATE below. Every job not yet terminal is
            # replaced wholesale with a fresh dict if it resolved this round.
            parse_jobs = []
            for job in run.parse_jobs or []:
                if job["outcome"] is not None:
                    parse_jobs.append(job)
                    continue
                saq_job = await get_parse_job(job["job_key"])
                if saq_job is None:
                    parse_jobs.append(job)  # expired/unknown -- keep polling until the deadline
                elif saq_job.status not in TERMINAL_STATUSES:
                    parse_jobs.append(job)
                elif saq_job.status == Status.COMPLETE:
                    parse_jobs.append(await _apply_outcome(ds_repo, job, saq_job.result or {}))
                else:
                    # FAILED/ABORTED on the SAQ job itself (not a ParseError,
                    # which the parser returns as a normal "rejected" result
                    # dict, never a raised exception on the queue).
                    parse_jobs.append(
                        {**job, "outcome": "rejected", "code": "job_" + saq_job.status.value}
                    )

            timed_out = loop.time() >= deadline
            all_terminal = all(job["outcome"] is not None for job in parse_jobs)

            if all_terminal or timed_out:
                final_status, error_message = _final_status(parse_jobs, timed_out)
                job_comments = _build_job_comments(parse_jobs, timed_out)
                await run_repo.update_progress(
                    run_id,
                    status=final_status,
                    parse_jobs=parse_jobs,
                    error_message=error_message,
                    job_comments=job_comments,
                )
                await HumanAuditRepo(session).append(
                    {
                        "org_id": org_id,
                        "actor_id": "Internal System",
                        "actor_email": "Internal System",
                        "event_type": "analysis_parsing_completed",
                        "deal_id": deal_uuid,
                        "payload": {
                            "analysis_run_id": analysis_run_id,
                            "status": final_status,
                            "parse_jobs": parse_jobs,
                            "job_comments": job_comments,
                        },
                    }
                )

                if final_status == "successful":
                    # Chain straight into ingest+verify -- no job_name="extraction"
                    # row, since extraction already happened inside this job's own
                    # per-document calls (point 4). Inline, same worker, same
                    # pattern as the fan-out above -- no reconciler (D9/D2 of the
                    # stage-chaining plan). The row is created here (inside this
                    # transaction, so it's covered by the same commit as
                    # everything else above); the actual queue enqueue is
                    # deliberately deferred until *after* this transaction
                    # commits (see below, outside the `async with`) -- enqueuing
                    # here would let another worker dequeue and start
                    # start_deal_verification, doing its own SELECT for this
                    # row, before this transaction's INSERT is even durable
                    # (real bug hit in local testing: "analysis_run ... not
                    # found" because the SAQ dequeue outraced the Postgres
                    # commit).
                    verification_run_id = uuid4()
                    verification_repo = AnalysisRunRepo(session)
                    await verification_repo.create(
                        {
                            "id": verification_run_id,
                            "org_id": org_id,
                            "deal_id": deal_uuid,
                            "job_name": "verification",
                            "status": "queued",
                        }
                    )
                    await HumanAuditRepo(session).append(
                        {
                            "org_id": org_id,
                            "actor_id": "Internal System",
                            "actor_email": "Internal System",
                            "event_type": "analysis_requested",
                            "deal_id": deal_uuid,
                            "payload": {
                                "analysis_run_id": str(verification_run_id),
                                "job_name": "verification",
                            },
                        }
                    )
                break

            await run_repo.update_progress(run_id, parse_jobs=parse_jobs)

        await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    # Outside the transaction -- verification_run_id's row (if any) is now
    # durably committed, so a worker that dequeues this immediately is
    # guaranteed to find it.
    if verification_run_id is not None:
        await get_queue().enqueue(
            "start_deal_verification",
            analysis_run_id=str(verification_run_id),
            parsing_run_id=analysis_run_id,
            clerk_org_id=clerk_org_id,
            timeout=7200,
            retries=1,
            ttl=86400,
        )
