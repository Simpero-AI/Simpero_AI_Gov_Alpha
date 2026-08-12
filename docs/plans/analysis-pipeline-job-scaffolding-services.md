# `process_document` — Implementation Handoff to `Simpero_Gov_AI_Services`

> **Status: SUPERSEDED — `process_document` is built and merge-ready,
> `Simpero_Gov_AI_Services` PR #49.** kpal002 confirmed PR #49's worker
> consumes Alpha's enqueue exactly (`spaces_key`, `entity`,
> `known_sha256s=None`, `audit=True`) and reviewed PR #81's ingest step
> against it — contract matches, chain is complete. **The "What to
> implement" sketch below is historical and has two known bugs** (it passed
> `known_sha256s` to `extract_claims`, which isn't one of that function's
> parameters, and omitted `extract_claims`'s required `run_id`/
> `correlation_id` arguments) — both already fixed correctly in the real
> PR #49 implementation. Left in place only so the reasoning sections below
> (why `audit=True` always, why `known_sha256s` is always empty, result
> delivery) still have something concrete to anchor to — **do not copy the
> code sketch**, it does not match what actually shipped.
>
> **What's actually still open, as of PR #49 landing:**
> 1. **`ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` in the parser worker's
>    deploy environment** — per kpal002, this is the live blocker now.
>    `audit` is unconditionally `True` on every call, so there is no
>    key-free path; every document fails closed until this is set.
> 2. **Sequencing**: bring the `Simpero_Gov_AI_Services` worker up
>    *before* `Simpero_AI_Gov_Alpha` PR #81 starts enqueuing — otherwise
>    jobs queue up unconsumed (the same silent-drop risk this doc
>    described from the other direction, back when #49 didn't exist yet).
>
> Original framing below, for the historical record and the still-valid
> reasoning sections.
>
> Companion to `docs/plans/analysis-pipeline-stage-chaining.md` (reviewed by
> `kpal002` on PR #81 of `Simpero_AI_Gov_Alpha`).

---

## tl;dr — what to build

One new SAQ job, `process_document`, registered in `worker.py` alongside
`parse_document`. It fetches a document from Spaces, calls the existing
`extract_claims(..., audit=True)` (nothing new — that function is already
complete and already what `POST /extract` calls), and writes the result
back to Spaces as a pointer, same shape as `parse_document` does today.
That's the whole job. Sections below give the exact signature, the two
things that are **hard blockers** if missed, and how to verify it yourself
before Alpha's queue ever reaches it.

---

## Hard blockers — nothing works without these two

### 1. `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` must be set in the **worker's** environment

Not just wherever `POST /extract` runs — the SAQ worker process
specifically, which may be a different deployment target. This is not
optional or best-effort: `extract_claims` fails closed
(`ProseCredentialMissing`) the moment `audit` is truthy, **even with every
other tier off**:

```python
# extract_service.py — the actual check
if (want_prose or canonicalize_attributes or audit) and not api_key_present():
    raise ProseCredentialMissing(...)
```

Alpha calls this job with `audit=True` on **every single call, unconditionally**
(see "Why `audit=True` always" below). So: no credential in the worker's
env → every document fails before parsing even starts, forever, with no
partial success possible. Confirm this before anything else here.

### 2. The job must be named exactly `"process_document"`, with exactly these keyword argument names

SAQ dispatches by function name with **zero schema validation across the
two codebases** — a mismatch in either the function name or a kwarg name
is silent on both sides: Alpha enqueues, nothing ever picks it up, no error
anywhere (the exact failure mode this repo's own `CLAUDE.md`/README already
document for the `parse_document` contract — same risk, new job name).

---

## The exact contract

**What Alpha sends** (`app/jobs/parse_client.py::enqueue_process_document_job`,
already shipped):

```python
job = await get_parse_queue().enqueue(   # the "parse" queue -- unchanged
    "process_document",
    spaces_key=spaces_key,          # str -- same Spaces object parse_document reads today
    entity=entity,                  # str -- Deal.name, see Open Questions
    known_sha256s=known_sha256s,    # always None from Alpha (D12 — see below)
    audit=True,                     # always True, never False, from Alpha
)
```

**What to implement**, in `worker.py`, alongside `parse_document`:

```python
async def process_document(
    ctx: Context,
    *,
    spaces_key: str,
    entity: str,
    known_sha256s: list[str] | None = None,
    audit: bool = True,
) -> dict:
    """Combined parse + claim-extraction + binding-audit job. Fetches
    spaces_key the same way parse_document does, then calls extract_claims
    -- the same entry point POST /extract already calls, so the two can
    never drift. Writes the resulting claims envelope to Spaces and returns
    a {bucket, key} pointer, never the payload inline (see Result delivery).
    """
    client = build_spaces_client(parser_settings)
    if client is None:
        raise RuntimeError("Spaces is not configured -- worker cannot fetch source documents")

    try:
        obj = client.get_object(Bucket=parser_settings.spaces_bucket, Key=spaces_key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"NoSuchKey", "404"}:
            return {"status": "rejected", "code": "source_not_found", "message": f"{spaces_key}: {exc}"}
        raise

    data = obj["Body"].read()

    # CORRECTED from the original sketch (kpal002's review on PR #81):
    # extract_claims has no known_sha256s parameter at all -- that's
    # parse_pdf_bytes's own dedupe-rejection argument, and extract_claims
    # calls parse_pdf_bytes(data) internally without forwarding it, so this
    # job's known_sha256s kwarg is accepted (Alpha's contract sends it) but
    # not something extract_claims itself takes. run_id/correlation_id ARE
    # required by extract_claims and were missing from the original sketch
    # -- Alpha doesn't send either, so generate them here (e.g. the SAQ
    # job's own key, or a fresh uuid4 per call).
    try:
        payload = extract_claims(
            data, entity=entity, run_id=ctx["job"].key, correlation_id=str(uuid4()), audit=audit,
        )
    except ParseError as exc:
        return {"status": "rejected", "code": exc.code, "message": exc.message}
    except ProseCredentialMissing as exc:
        # Fails the SAQ job outright rather than returning a soft "rejected" --
        # this is a deployment/config problem, not a bad document, and every
        # subsequent call will fail the same way until fixed. Don't let it
        # look like a per-document rejection.
        raise

    results_key = f"{parser_settings.results_key_prefix.rstrip('/')}/{payload['sha256']}.json"
    client.put_object(
        Bucket=parser_settings.spaces_bucket,
        Key=results_key,
        Body=json.dumps(payload).encode(),
        ContentType="application/json",
        ServerSideEncryption="AES256",
    )

    return {
        "status": "parsed",
        "bucket": parser_settings.spaces_bucket,
        "key": results_key,
        "sha256": payload["sha256"],
        "count": len(payload["claims"]),
    }
```

This is a **sketch**, not a literal patch — check it against this repo's
actual current `parse_document` implementation and adapt (error handling
idioms, logging, exact `parser_settings` fields) rather than pasting it
verbatim. The two things that must not change from the sketch: the return
shape (`{status, bucket, key, ...}` on success, `{status: "rejected", code,
message}` on rejection — **identical shape to `parse_document`'s own
contract**, since Alpha's polling/outcome code (`_apply_outcome` in
`start_deal_analysis.py`) already handles exactly this shape and nothing
about it changed), and that `extract_claims` is called with `audit=True`
unconditionally, not gated on the caller's `audit` param being a real
choice.

### Why `audit=True` always

The binding audit (`verify.py::audit_claim`) needs the same in-memory
`pages` (char-geometry index) that `extract_claims` already produces by
calling `parse_pdf_bytes` internally. A caller-optional `audit` flag would
be pointless from Alpha's side — Alpha never calls with `audit=False`, and
there is no other caller of `process_document` — but the parameter exists
so `process_document` isn't hardcoded around one caller's behavior.

### Why `known_sha256s` is always empty/`None` from Alpha

It's a duplicate-**rejection** list on this repo's side (`docling_parser.py`
raises `ParseError("duplicate_pdf", ...)` for any hash present in it) — not
a "here's what I already have" hint. Deal-level dedupe already happens on
Alpha's side at presign time. Passing the document's own hash here would
make every single call reject itself. This isn't new — `parse_document`
already has the identical constraint (`D12` in the stage-chaining plan);
`process_document` inherits it unchanged.

### Result delivery — Spaces pointer, not inline

`extract_claims`'s real payload (`{run_id, sha256, source_file, claims,
edges, flag_log, skipped_pages}`) is large for a real filing — hundreds of
claims with spans, tables, flags. SAQ job results have their own size/ttl
limits, and Valkey isn't meant to hold this. Mirror `parse_document`'s own
existing reasoning exactly: write to Spaces, return a pointer. This was an
open question in the first draft of the Alpha-side plan; it's decided now,
not something to re-litigate.

### Register it

```python
# worker.py
settings: SettingsDict = {
    "queue": queue,
    "functions": [parse_document, process_document],  # add process_document
    "before_process": _normalize_job_policy,  # see below -- needs its own version
    "concurrency": 1,
}
```

Keep `parse_document` registered if anything else in this repo still calls
it directly (e.g. the standalone `POST /parse` route is unaffected by any
of this). Alpha's fan-out no longer calls it either way.

### `before_process` — needs its own timeout, don't reuse `parse_document`'s

`parse_document`'s hook sets `timeout=1800, retries=2`. **Do not reuse those
numbers unmeasured.** `process_document` additionally calls Anthropic (the
binding audit, always; whichever prose/qualitative/canonicalization tiers
Alpha eventually turns on — today it calls with defaults, i.e. table-only
claims plus the audit) — a completely different latency and failure profile
than docling's CPU-bound parse alone. Real numbers needed, informed by
actual measured latency at whatever `concurrency` this ends up running at.
`concurrency: 1` (docling is memory-heavy) is a reasonable starting point
since the parse step is still in the same process — confirm, don't assume.

---

## How to verify this yourself, before Alpha's queue ever reaches it

You don't need Alpha's stack running to prove `process_document` works —
it's the same `extract_claims` call `scripts/emit_claims.py` and
`POST /extract` already exercise, just wrapped for the queue.

1. **Unit-level**: call `process_document({}, spaces_key=..., entity="Test
   Corp")` directly against a real Spaces object you control, same as this
   repo's existing tests exercise `parse_document`. Assert the return shape
   matches `parse_document`'s (`{status, bucket, key, ...}` / `{status:
   "rejected", code, message}`), and that the object at `{bucket, key}` on
   success actually contains a valid claims envelope (`{run_id, sha256,
   source_file, claims, edges, flag_log, skipped_pages}` — the same shape
   `contracts/claims.schema.json` validates, since Alpha's ingest step
   validates against that exact schema and will reject anything that
   doesn't match).
2. **Credential check**: run once with `ANTHROPIC_API_KEY` unset in the
   worker's env deliberately, confirm it raises (not returns a rejected
   result) — this must be a hard failure, not something that silently
   produces empty claims.
3. **Queue-level, once satisfied with (1)**: `saq worker.settings` locally,
   enqueue a `process_document` job by hand (same Valkey instance Alpha's
   dev stack uses — `docker-compose.dev.yml` in `Simpero_AI_Gov_Alpha`
   exposes it), confirm the job resolves `COMPLETE` with the right result
   shape via `queue.job(key)`.

---

## Resolved (this doc's original open questions)

- `entity = Deal.name` — shipped as-is; not revisited.
- `before_process` timeout/retries/concurrency — set in PR #49; not
  re-litigated here.
- Whether `verification` needs its own job on this side: **no**, confirmed
  on PR #81 review. It's Alpha's `reconcile_same_fact`/`reconcile_consistency`,
  entirely outside this repo, run over claims Alpha has already ingested —
  see `docs/plans/analysis-pipeline-stage-chaining.md` for the two follow-up
  items filed against that step (deal-level reconciliation scope; ingest
  idempotency — both Alpha-side, not this repo's).
- Whether the binding audit needs a standalone entry point: **no**.
  `audit=True` on the one combined call covers it.
- Whether parsing and extraction should stay two separate jobs: **no**,
  combined into `process_document`, confirmed correct and shipped.

---

## Definition of done

- [x] `process_document` implemented in `worker.py` — PR #49.
- [x] Registered in `functions` — PR #49.
- [x] Contract confirmed matching Alpha's enqueue — kpal002, PR #81 review.
- [ ] `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` confirmed present in the
      actual worker deploy environment — **the live blocker**.
- [ ] Worker brought up and confirmed consuming before PR #81 merges and
      starts enqueuing for real.
