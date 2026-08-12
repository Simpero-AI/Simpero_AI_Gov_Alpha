# Start Analysis → Parse/Extract/Audit → Ingest → Verify — Implementation Summary

**Status:** Implemented and committed on branch `SIM-399` (`90f237c`, `cc09cf5`, `828f875`, `0f02893`), open as PR #81 on `Simpero_AI_Gov_Alpha`, pushed. Reviewed twice by `kpal002` (`Simpero_Gov_AI_Services`' owner) — first review reshaped the design mid-implementation (see "Reworked after review" below), second confirmed the contract against that repo's own `process_document` implementation (PR #49, merge-ready). **The full pipeline has now actually completed end-to-end for real** — real Docling parse, real Anthropic-backed extraction/audit, real ingest and reconciliation against a genuine CIM PDF — the first time this has happened outside a mocked test (see "First real local end-to-end run" below). **Not yet merged** — see "What's still needed" at the end.
**Plan followed:** `docs/plans/start-analysis-flow-alpha.md` (original design) and `docs/plans/analysis-pipeline-stage-chaining.md` (the post-review redesign — that doc is now itself the more current design record; this doc is what actually shipped from both).
**Session:** 2026-08-10 (original build) through 2026-08-12 (rework, review response, and a full local end-to-end run against real `Simpero_Gov_AI_Services`/Anthropic).

---

## What this feature is

Replaces the dead `/api/simpero/analyse` call the frontend's Step 3 ("Start Analysis") used to make, with a real, multi-stage pipeline:

1. `POST /api/deals/{deal_id}/analysis` creates an `analysis_run` row (`job_name="parsing"`) and enqueues `start_deal_analysis`, this app's own worker task.
2. That task fans the deal's verified documents out to `Simpero_Gov_AI_Services`' `"parse"` queue — one **combined** parse+extract+binding-audit job per document (`process_document`, not the older parse-only `parse_document`) — waits for every one to finish, and records outcomes onto `analysis_run.parse_jobs`/`job_comments` (and, for scanned/unreadable documents, `data_source.status = "ocr_needed"`).
3. On success, it creates a second `analysis_run` row (`job_name="verification"`) and enqueues `start_deal_verification`, a second worker task: reads each document's claims envelope back from Spaces, ingests it into the `claims`/`edges` spine under RLS, then runs the deal's already-built 3a/3b reconciliation passes (`reconcile_same_fact`, `reconcile_consistency`) over what was just ingested.

`GET /deals/{deal_id}/status` reports real progress through both stages instead of the permanent `no_job` stub, mapped through the frontend's step vocabulary (reduced to the two real phases, `parsing`/`verification`, on 2026-08-12 — see "Known, named gaps" below for why).

**`Simpero_Gov_AI_Services` *did* change**, unlike the original build's assumption — `process_document` is new work there (PR #49), built in response to this repo's handoff doc after a design review found the original split (a parse-only job, then a separate extract job) was solving a problem that didn't exist (see "Reworked after review").

---

## Reworked after review — the design changed mid-implementation

The first pass (2026-08-10) shipped `job_name` with three declared-but-mostly-unused values (`parsing`/`extraction`/`verification`) and a plan to wire `extraction`/`verification` up later. Before that happened, `kpal002` reviewed the plan on PR #81 and found three things wrong with it, all verified against real code before being accepted:

1. **The planned split (separate parse job, separate extract job) was unnecessary.** `Simpero_Gov_AI_Services`' `parse_pdf_bytes` is read-through cached by SHA-256, storing the full `DoclingDocument` — a second call from a separate extract job would have been a cache read, not a second docling run. **Fix:** one combined `process_document` job per document (parse + `extract_claims(audit=True)` in one call). `job_name="extraction"` is now confirmed **unused** — no row is ever created with it; left in the `CHECK` constraint harmlessly rather than migrated away.
2. **`job_name="verification"` was conflated with the parser's binding audit.** Folding the audit into the combined job via `audit=True` is correct (it needs the same in-memory page geometry `extract_claims` already produces). But "verification" in the product sense is this repo's **own** cross-claim reconciliation (`app/services/reconciliation.py::reconcile_same_fact`, `app/services/consistency.py::reconcile_consistency` — SIM-371/372, already built, never previously wired to anything async) — a deal-level pass over **ingested** claims, not a parser call at all. **Fix:** `job_name="verification"` now maps to a real new task, `start_deal_verification.py`.
3. **Nothing ingested the claims — the pipeline dead-ended before the database.** The original design recorded a `{bucket, key}` pointer to a claims envelope on Spaces and stopped. Nothing read it back or inserted it into `claims`/`edges`, so 3a/3b would have had nothing to run on even once built. **Fix:** `start_deal_verification.py`'s first phase is exactly this — the async equivalent of the already-existing `scripts/ingest_claims.py`.

Full reasoning, file:line citations, and the two docs `kpal002` wrote (`stage-chaining-suggested-changes.md`, the PR comment) are preserved in `docs/plans/analysis-pipeline-stage-chaining.md`, which now supersedes this doc as the primary design record — read that first for *why*; this doc is *what shipped*.

**A second, narrower review round** (after the rework landed) confirmed the contract against `Simpero_Gov_AI_Services`' real `process_document` implementation (PR #49) and found one more real bug, fixed in the same session — see "Bugs found and fixed," #2.

---

## The one open DDL decision, closed (unchanged from the original build)

The plan flagged a blocking DDL choice: `data_source`'s one-way status trigger (added when SIM-216/218 shipped) rejected any UPDATE once a row left `pending`, which meant the parser's `no_extractable_text` signal (SIM-350) could never legally write `verified → ocr_needed`. **Vansh chose Option A**: relax the trigger to allow that one additional edge. `alembic/versions/92fda2e2a5db_data_source_ocr_needed_transition.py` — `pending → anything` still allowed, `verified → ocr_needed` now also allowed, everything else still raises, still enforced against every role including the table owner.

---

## `analysis_run` schema — final shape

Two rounds of revision, both folded directly into the not-yet-shipped `3fd6292e23f0_analysis_run.py` migration rather than layered as separate migrations (nothing had shipped, so no reason to carry the churn):

- **`created_at` → `started_at`**, plus new **`ended_at`** (nullable, stamped server-side by `AnalysisRunRepo.update_progress` the one time `status` becomes terminal).
- **`status`'s four values**: `queued` → `in_progress` → `successful`|`failed`. (Two adjacent vocabularies deliberately left alone: the UI phase name `"parsing"` in `pipeline_steps.py`, and the parser's own per-document `parse_jobs[].outcome` vocabulary (`"parsed"`/`"rejected"`) — both unrelated to this column.)
- **`job_comments`** (JSONB, nullable) — frontend-facing findings summary, one entry per document, camelCase, populated at the same moment `ended_at` is stamped. A rejected entry's `comment` is the parser's own `message` verbatim when it has one (persisted onto `parse_jobs[].message` by `_apply_outcome`); this app's own wording is only a fallback for a "parsed" success (no narrative field on the parser's side) or a SAQ-level job failure (never reached the parser's error path at all).
- **`job_name`** (Text, `CHECK`'d to `parsing`/`extraction`/`verification`, `server_default 'parsing'`, append-only) — see "Reworked after review": only `parsing` and `verification` are ever actually created.
- `uq_analysis_run_active` — partial unique index, `(deal_id) WHERE status IN ('queued','in_progress')`. Deliberately **not** scoped to `(deal_id, job_name)` — at most one active run per deal, full stop, since the two job types run strictly sequentially (verification only ever starts after parsing succeeds), never concurrently.
- `REVOKE UPDATE, DELETE ... FROM dd_app` then `GRANT UPDATE (status, parse_jobs, error_message, job_comments, ended_at, updated_at) ... TO dd_app` — everything else (`org_id`/`deal_id`/`job_name`/`selected_frameworks`/`started_at`/`id`) stays append-only.
- **Deliberately no one-way trigger** (unlike `data_source`) — this table's whole point is walking a real multi-step lifecycle, not enforcing a single legitimate transition.

---

## What was built

### Database (two migrations)

- `92fda2e2a5db_data_source_ocr_needed_transition.py` — the trigger relaxation above.
- `3fd6292e23f0_analysis_run.py` — the `analysis_run` table, final shape above.

### Python modules

| File | Purpose |
|---|---|
| `app/models/analysis_run.py` | `AnalysisRun` ORM model |
| `app/repo/AnalysisRunRepo.py` | `create`, `get_by_id`, `latest_for_deal`, `active_for_deal`, `update_progress` (`SELECT ... FOR UPDATE`) |
| `app/jobs/tasks/start_deal_analysis.py` | Parsing stage — fan-out, poll, terminal write, **chains into verification on success** |
| `app/jobs/tasks/start_deal_verification.py` | Verification stage — ingest + 3a/3b, **new this session** |
| `app/jobs/parse_client.py` | `enqueue_process_document_job` (replaced `enqueue_parse_job`), `get_parse_job` (unchanged, generic) |
| `app/services/uploads/spaces.py` | `get_json_object` (new) — reads the claims envelope back from Spaces |
| `tests/test_analysis_run_rls.py`, `test_start_analysis_endpoint.py`, `test_start_deal_analysis_job.py`, `test_start_deal_verification_job.py` | Full coverage, both stages |

**Modified, not new:** `app/repo/DataSourceRepo.py` (`list_for_deal`), `app/schemas/deals.py` (`StartAnalysisRequest`, `JobCommentResponse`, `DealStatusResponse.job_comments`), `app/api/deals.py` (`POST .../analysis`, `_steps_for_status` now `(job_name, status)`-keyed), `app/jobs/tasks/__init__.py` (both tasks registered), `tests/test_data_source_rls.py` (trigger-relaxation coverage).

### API surface (final)

- **`POST /deals/{deal_id}/analysis`** — unchanged contract from the original build (`{selectedFrameworks?}` → `202` + `DealStatusResponse`). Internally now always creates `job_name="parsing"` explicitly (hardcoded — this handler only ever creates parsing runs; verification runs are created by the worker, not the API).
- **`GET /deals/{deal_id}/status`** — `_steps_for_status` now keyed by `(job_name, status)`, not `status` alone:

  | `job_name` | `status` | `jobStatus` | `currentPhase` |
  |---|---|---|---|
  | *(no run)* | — | `no_job` | `null` |
  | `parsing` | `queued` | `queued` | `null` |
  | `parsing` | `in_progress` | `processing` | `"parsing"` |
  | `parsing` | `successful` | `processing` | `"verification"` (extraction+audit already happened inside this same job) |
  | `parsing` | `failed` | `error` | `"parsing"` (+ `errorMessage`) |
  | `verification` | `queued`/`in_progress` | `queued`/`processing` | `"verification"` |
  | `verification` | `successful` | `processing` | `"governance"` |
  | `verification` | `failed` | `error` | `"verification"` (+ `errorMessage`) |

  Never maps to `"complete"` at any point — the memo tail (`governance` → `scoring`) has no job behind it yet. (`current_phase`/step-key value renamed `pass2` → `verification` on 2026-08-12, after watching the real status page against a real run — clearer than the legacy mock-backend name it was inherited from; propagated through `_steps_for_status`, `DealStatusResponse`, and `pipeline_steps.py` on both repos.)

  `DealStatusResponse` also carries `started_at` (the whole chain's start — the parsing run's own `started_at`, even once the latest row is the verification run), `ended_at` (the latest run's own `ended_at`, null while still running), and `step_durations` (real per-step wall time in seconds, keyed `"parsing"`/`"verification"`, present only once that step's own run has actually ended) — added so the frontend's elapsed timer can freeze at a real value instead of ticking forever, and each finished step can show its own real duration instead of nothing or a guess.

---

## How the parsing stage works (`start_deal_analysis`)

Same two-phase shape as the original build, with the fan-out target changed and a new final step:

**Phase 1 — fan-out:** loads the run, loads the `Deal` (for `entity=deal.name`), lists verified documents, and for each not already recorded, calls `enqueue_process_document_job(storage_key, entity=deal.name, known_sha256s=None)` — **not** `enqueue_parse_job`/`parse_document` anymore. Records `{data_source_id, filename, storage_key, job_key, outcome: null, code: null, message: null, bucket: null, key: null}` per document, writes `status="in_progress"`.

**Phase 2 — poll to terminal:** unchanged polling shape (new transaction per 15s poll, never held across the wait). Applies outcomes, writes `ocr_needed` on `no_extractable_text`, builds `job_comments`. **New:** on `final_status == "successful"`, creates a `job_name="verification"` `analysis_run` row and enqueues `start_deal_verification` on the `"simpero"` queue — inline, same worker, same transaction pattern as the fan-out itself, no reconciler. A `failed` parsing run does **not** chain into verification — nothing to verify.

---

## How the verification stage works (`start_deal_verification`, new)

No external async wait (ingest and reconciliation are synchronous DB/Spaces work), so — unlike the parsing stage — the **whole job runs in one transaction**, committed once at the end:

1. Load the verification run and the parsing run it follows.
2. **Idempotency guard** (added after the second review round — see "Bugs found and fixed"): if the verification run has already reached a terminal status, return immediately. Guards against a SAQ redelivery re-running the insert-only ingest after a successful commit.
3. Filter the parsing run's `parse_jobs` to `outcome == "parsed"` — anything rejected (needs OCR, etc.) is skipped, not ingested. If none, the run fails with `"No documents were successfully extracted to verify."`
4. For each usable document: `get_json_object(bucket, key)` reads the claims envelope back from Spaces, validates it against `contracts/claims.schema.json` (same schema `scripts/ingest_claims.py` validates against), inserts `Claim` rows (with real `deal_id`/`data_source_id`, unlike the demo script's NULLs) and the envelope's own extraction-reducer `Edge` rows, under RLS.
5. Once every document is ingested: for each `data_source_id`, calls `reconcile_same_fact` then `reconcile_consistency` — **scoped per document**, not per deal (see "Known gap" below — this is Vansh's explicit call, not an oversight).
6. Builds a `job_comments`-shaped summary per document (claims ingested, edges written, reconciliation edge counts), writes the terminal `status`/`job_comments`, appends a closing `human_audit_log` row (`event_type="analysis_verification_completed"`).

---

## The Valkey contract: what actually gets picked up, by whom (updated)

Same two-queue shape as the original build (`"simpero"` for this app's own tasks, `"parse"` for the cross-service hop) — the cross-service payload changed:

```python
# app/jobs/parse_client.py::enqueue_process_document_job
job = await get_parse_queue().enqueue(
    "process_document",              # was "parse_document"
    spaces_key=spaces_key,
    entity=entity,                   # NEW -- Deal.name, required by extract_claims
    known_sha256s=known_sha256s,     # always None from this app, unchanged reasoning
    audit=True,                      # NEW -- always True, never a caller option
)
```

Confirmed by `kpal002` against the real `Simpero_Gov_AI_Services` PR #49 implementation — function name and every kwarg name match exactly. **Return shape is unchanged** from the old `parse_document` contract (`{status: "parsed", bucket, key, sha256, count}` / `{status: "rejected", code, message}`) — only what's *at* the `{bucket, key}` pointer changed, from a bare `ParseResponse` to the richer `extract_claims` payload (`{run_id, sha256, source_file, claims, edges, flag_log, skipped_pages}`). This is why `start_deal_analysis`'s outcome-recording code (`_apply_outcome`, `_build_job_comments`) needed almost no changes — the polling contract it was already built against didn't change shape, just what the pointer resolves to.

---

## First real local end-to-end run (2026-08-12)

Everything up to this point had been proven either by mocked/direct-call tests (Tier 1 of `docs/local-testing-guide.md`) or by `kpal002`'s code review — never by a real job actually running the real pipeline against a real document. This session did that: local Postgres/Valkey/app/worker (Alpha), a real `Simpero_Gov_AI_Services` worker (`uv run saq parser_service.worker.settings`, PR #49's `process_document`, real Docling + real Anthropic), and the real frontend, wired together and driven by hand through the actual New Deal wizard.

Getting there surfaced a run of real gaps, none of which any existing test caught (each is either infra the test suite can't see, or a difference between the dev `.venv` and the built Docker image):

- **CORS.** The shared Spaces bucket's CORS policy never had `http://localhost:5173` as an allowed origin, so the browser's presigned-PUT preflight got a 403. Fixed by adding the origin to the bucket's CORS rules directly (infra, not code).
- **`Simpero_Gov_AI_Services`' `.env` never actually reached `os.environ`.** `ParserSettings`' `pydantic-settings` `env_file=".env"` support only ever populates that class's own `PARSER_`-prefixed fields — `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` are deliberately *not* fields there (read straight from `os.environ`, matching the Anthropic SDK's own convention), so dropping them in `.env` was a silent no-op and the worker kept failing closed with `ProseCredentialMissing` even after the key was added. Fixed on that repo's side: `parser_service/config.py` now calls `load_dotenv()` at import time; `python-dotenv` promoted from a transitive dep to a declared one. `.env.example` there also gained the previously-undocumented `PARSER_VALKEY_URL` and `ANTHROPIC_API_KEY` entries — neither had ever been written down.
- Four real bugs in *this* repo's own code, once the above cleared the way — see "Bugs found and fixed" below (#3–#6).

Once all of those were cleared, a real analysis run on a real deal (`CIM-03-Bar-Wash.pdf`) went all the way through for the first time: real Docling parse → real Anthropic-backed `extract_claims(audit=True)` → **3,255 claims ingested**, **2 `same_fact`** and **76 `contradicts`** edges from `reconcile_same_fact`, 0 from `reconcile_consistency` — independently confirmed against Postgres directly (`SELECT count(*) FROM claims ...`, `SELECT type, count(*) FROM edges GROUP BY type`), not just trusted from `job_comments`. `GET /status` correctly reported `currentPhase: "governance"` with both steps `done` once it finished. (A first document, `CIM-01-Consolidated-Utility-Services.pdf`, correctly hit the `no_extractable_text`/`ocr_needed` path instead — a genuinely scanned/image-only PDF, not a bug; see the "one open DDL decision" section above for why that transition is even legal.)

---

## Bugs found and fixed (via testing/review, not by inspection)

**1. In-place JSONB mutation silently defeated SQLAlchemy's dirty-tracking** (original build, 2026-08-10). The polling loop mutated each `parse_jobs` dict in place before reassigning; since the dicts loaded from the DB earlier in the same transaction were the *same Python objects*, SQLAlchemy compared "old" against "new" at flush time, found them identical, and silently skipped the `UPDATE` — parse outcomes would never have persisted. Caught by `tests/test_start_deal_analysis_job.py` against real Postgres (a pure-mock test would not have caught this — it's specifically about SQLAlchemy's change-tracking on a JSONB column). Fixed by returning **new** dicts instead of mutating.

**2. Verification's ingest had a real, if narrower, idempotency gap than first flagged** (`kpal002`'s second review round, 2026-08-12). A `ponytail:` comment originally warned about a mid-job-crash retry violating `uq_claims_org_data_source_claim_ref` — overstated, since the whole job is one transaction and a mid-job crash already rolls back cleanly. The real exposure: a SAQ redelivery **after** a successful commit would re-run the insert-only ingest and hard-fail on that same constraint, leaving the run stuck. Fixed with a terminal-state guard at the top of `start_deal_verification` (mirrors `start_deal_analysis`'s own D11 idempotency pattern), covered by a new test (`test_already_terminal_run_is_a_noop`).

**3. Enqueue-before-commit race** (found live, first real run, 2026-08-12). `start_deal_analysis`'s terminal branch created the `job_name="verification"` row and called `get_queue().enqueue("start_deal_verification", ...)` **inside** the same still-open transaction — the Valkey enqueue is a completely independent side effect from the Postgres commit, so a fast worker could dequeue and `SELECT` for that row before the `INSERT` was even durable. Manifested exactly as `ValueError: analysis_run ... not found`, immediately, every time, in the real worker's logs — not a rare race, a near-guaranteed one given how fast SAQ redelivers. Fixed by deferring the `enqueue()` call until after the `async with AsyncSessionLocal() as session, session.begin():` block exits (i.e. after commit) — the row is created and audited inside the transaction as before, only the queue write moved.

**4. `jsonschema` was declared as a dev-only dependency.** `start_deal_verification.py` imports it directly (`_validate_claims`, real production code, runs in the worker) — but it lived under `[dependency-groups].dev` in `pyproject.toml`, which `Dockerfile`'s `uv sync --frozen --no-dev` never installs. Every local `pytest` run installs dev deps too, so this was invisible until the real Docker-built worker container hit `ModuleNotFoundError: No module named 'jsonschema'` on its first real verification job. Moved to `[project.dependencies]`.

**5. `contracts/claims.schema.json` was never copied into the Docker image at all.** Same job, same run, next failure once #4 was fixed: `FileNotFoundError: /app/contracts/claims.schema.json`. `Dockerfile` copied `app/`, `alembic.ini`, `alembic/` but never `contracts/`, which `start_deal_verification.py` reads by path at runtime. Added `COPY contracts/ /app/contracts/` alongside the other `COPY` lines.

**6. A duplicate-hash upload was a dead end, not a duplicate.** `POST /uploads/presigned-url`'s 409 ("this file's already uploaded for this deal") returned a bare string; the frontend's `runDocumentUpload` pipeline turned that into a hard error with no forward path — hit for real when retrying analysis after the OCR-needed rejection above required re-entering the wizard, and the wizard's local `hasUploadedDocument` state has no way to learn about a document that was already uploaded in an earlier session. Fixed on both sides: the 409's `detail` is now structured (`{message, dataSourceId, status}`), and `Simpero_AI_Gov_Web`'s pipeline catches `DuplicateUploadError` and resolves with the existing row's real `id`/`status` instead of throwing — a duplicate-by-hash upload is now exactly as successful, from the wizard's point of view, as a fresh one.

---

## Known, named gaps (not fixed here — flagged, not papered over)

- **Cross-document reconciliation.** `reconcile_same_fact`/`reconcile_consistency` are called once per `data_source_id`, not once per deal — a fact stated in two different documents of the same deal (the concrete example from review: a CIM says $50M revenue, an uploaded financial model says $52M for the same period) is never reconciled today. `kpal002` flagged this `[High]`, filed as a SIM-368 follow-up once Linear has room. **Checked directly with Vansh after the flag**: keeping the original "loop per document" call for now rather than building deal-level scope into this PR.
- **Long transaction in `start_deal_verification`.** Ingest + reconcile of every document in a deal runs inside one transaction, pinning one PgBouncer backend connection for the run's whole duration — unlike the parsing stage, which deliberately never holds a transaction across a wait. Assessed as fine at alpha scale by `kpal002`; revisit (commit per document, or chunk) if it becomes a real constraint.
- **The rest of the pipeline is still unbuilt**: nothing past `governance` (OFAC, memo drafting, mandate scoring, finalize) has a job behind it — `currentPhase` will sit at `"governance"` on a successful verification run and advance no further. (The UI step list itself was reduced from a 9-entry vocabulary to just the two real phases, `parsing`/`verification`, on 2026-08-12 — the old list included phases like `classify`/`pass1`/`ofac` that no job has ever set, which meant a step between two real checkpoints got silently marked "done" purely for sitting earlier in an index, not because anything ran it. See `docs/plans/analysis-pipeline-stage-chaining.md` for that fix's own writeup.) The entire chunking/embedding/retrieval lane (`parser_service/chunker.py`, `app/services/embedding.py`/`retrieval.py`) is a separate, parallel, entirely unbuilt branch, not touched by anything in this doc.

---

## Verification performed

- Fresh `docker-compose.dev.yml` volume (real Postgres 16 + pgvector, real Valkey), full migration chain (32 migrations as of this writing), reset and re-verified after every revision round, not just the first pass.
- `uv run pytest tests -q`: **289 passed**, 0 failures, on a freshly-migrated DB each time. (231 original baseline → 263 after the first build → 270 after the rework + idempotency fix → 289 after the real-run bug fixes + `verification` rename + timing fields.)
- `uv run pyright`: 0 errors throughout.
- `tests/test_start_deal_verification_job.py` runs the **real** `reconcile_same_fact` against genuinely-ingested claims (not mocked) — confirmed a real cross-page `same_fact` edge actually gets written, not just that the function gets called.
- Directly inspected `analysis_run`'s RLS policy, `FORCE`, column grants, partial unique index, and the relaxed trigger function body via `psql` — all match the design.
- **Beyond pytest**: a real end-to-end run through the actual Docker-built app/worker images against a real `Simpero_Gov_AI_Services` worker and real Anthropic API — see "First real local end-to-end run" above. This is the only verification in this doc that exercises the built container images rather than the source tree directly, which is exactly how it caught #4 and #5.

**Pre-existing, unrelated issue, still present:** `tests/test_retrieval_rls.py`/`test_memory_scope_rls.py` drop and replace the real `chunks` table without restoring it; running the suite twice against the same DB without a fresh migration leaves `chunks` gone for the second run. Confirmed unrelated every time it recurred, by resetting to a fresh DB.

---

## What's still needed (as of this writing)

**Alpha side:** nothing — PR #81 is complete, merge-ready, and has now been proven end-to-end against real `Simpero_Gov_AI_Services`/Anthropic locally (see "First real local end-to-end run").

**`Simpero_Gov_AI_Services` side:** `process_document` is built and locally proven (PR #49, merge-ready, contract confirmed, `load_dotenv()` fix landed there too). One item left before the *deployed* worker will actually run it:
1. `ANTHROPIC_API_KEY` needs to be added as a GitHub Actions secret in that repo's `staging` and `production` **Environments** (Settings → Environments → each → Secrets) — a manual step only Vansh can do. The workflow side of this is already wired (`deploy.yml`'s "Write .env from GitHub secrets" step now includes `ANTHROPIC_API_KEY=${{ secrets.ANTHROPIC_API_KEY }}`, and `docs/plans/parser-droplet-deployment.md`'s secrets table/checklist updated to match) — without the secret itself existing, the deployed worker will write an empty value and fail closed with the same `ProseCredentialMissing` this session hit locally before adding it to `.env`.
2. That worker brought up **before** PR #81 merges and starts enqueuing — otherwise jobs queue up unconsumed, silently, on either side.

**`Simpero_AI_Gov_Web` side:** the frontend rewire is done and locally proven against a real run (status card, elapsed timer, per-step durations, findings list, duplicate-upload recovery — all described above). Two small, independent gaps remain, found while checking that repo's own in-progress rewire against this contract — a stale e2e test (`analyse-async.spec.ts`) still targeting the dead legacy endpoint, and the pre-existing `Step2Materials.tsx`/`DealMaterialsDropzone.tsx`/`deriveMaterialsAutoTick.ts` files sitting deleted-but-uncommitted in that repo's working tree (superseded by `DealDocumentUpload.tsx`, confirmed unrelated to this feature, not yet committed one way or the other). Full detail on the first: `docs/plans/web-frontend-status-gaps-handoff.md`. Neither blocks PR #81.

Full design record, open questions, and the wireframe diagram of the complete pipeline: `docs/plans/analysis-pipeline-stage-chaining.md`. Services handoff, now marked superseded/historical now that PR #49 shipped: `docs/plans/analysis-pipeline-job-scaffolding-services.md`.
