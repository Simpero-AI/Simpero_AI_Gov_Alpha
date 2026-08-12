# Testing the client → API → services flow locally

Three tiers, cheapest/most-complete first. Tier 1 covers Alpha's own logic
fully today. Tiers 2–3 need `Simpero_Gov_AI_Services`' `process_document`
job to exist for the flow to actually complete — until it does, both will
stall at `queued`/`in_progress` the moment a document gets fanned out. That
stall is expected, not a bug; see the note in each tier.

---

## Tier 1 — automated, Alpha only (works today, no dependency on Services)

Covers: the API contract (404/409/422 branches), RLS, the migration, the
worker task logic (`start_deal_analysis`, `start_deal_verification`),
real ingestion into `claims`/`edges`, and a genuine `reconcile_same_fact`
integration (not mocked). Does **not** cover: the real SAQ worker process
picking a job off a real queue, or anything in `Simpero_Gov_AI_Services`.

```bash
cd Simpero_AI_Gov_Alpha

# Fresh Postgres + Valkey
docker compose -f docker-compose.dev.yml down -v
docker compose -f docker-compose.dev.yml up -d postgres valkey
# wait for postgres healthy, then:
docker compose -f docker-compose.dev.yml run --rm migrate sh -c "alembic upgrade head"

DATABASE_URL="postgresql+asyncpg://dd_app:sandbox_dd_app@localhost:5434/simpero" \
ALEMBIC_DATABASE_URL="postgresql+asyncpg://doadmin:sandbox_doadmin@localhost:5434/simpero" \
VALKEY_URL="redis://localhost:6381" \
uv run pytest tests -q

uv run pyright
```

Expect **269 passed**, 0 pyright errors (as of this session). Run this
first, always — it's the fast, deterministic check, and most regressions
show up here before they'd ever show up in a browser.

---

## Tier 2 — full stack, manual click-through (needs Services for the flow to *finish*, not to *start*)

This is the real "client → API → services" path, through the actual
frontend, real Clerk auth, real browser.

**1. Bring up Alpha's backend + worker:**

```bash
cd Simpero_AI_Gov_Alpha
docker compose -f docker-compose.dev.yml up --build
# app on :8000, worker running in its own container, postgres :5434, valkey :6381
```

Confirm `.env` has real `CLERK_*` values (this repo's `docker-compose.dev.yml`
`env_file: .env` — see that file's own comments) — Tier 2 uses real Clerk
auth, unlike pytest, which bypasses it via `dependency_overrides`.

**2. Bring up the frontend against it:**

```bash
cd Simpero_AI_Gov_Web
pnpm install
pnpm dev
```

`vite.config.ts` already proxies `/api` → `http://localhost:8000` — no env
change needed to point the frontend at the local backend. Log in through
the real Clerk flow.

**3. Walk the flow:**

1. New deal wizard → Step 1 (creates the deal via `POST /deals`) → Step 2
   (upload a document — this exercises the full presigned-URL pipeline,
   independent of anything in this doc) → Step 3 → Submit.
2. Submit calls `POST /deals/{dealId}/analysis`, then navigates to
   `/analysis/{dealId}`, which polls `GET /deals/{dealId}/status`.
3. **Expected today:** status sits at `queued` → `processing`/`"parsing"`
   and never advances further. Alpha's worker enqueued a `"process_document"`
   job onto the shared Valkey `"parse"` queue; nothing is listening for
   that name yet. This is the exact gap the services handoff doc
   (`docs/plans/analysis-pipeline-job-scaffolding-services.md`) exists to
   close — not something wrong with this test.
4. **What this tier *does* prove**, even stalled: the deal gets created,
   the document upload completes, `analysis_run` (`job_name="parsing"`)
   gets created with `status="queued"`, and the frontend's polling UI
   renders that state correctly. Check `docker compose logs worker` and the
   `analysis_run` row directly (`psql` into `localhost:5434`) to confirm
   the job was picked up by Alpha's own worker and the fan-out ran (parse
   jobs recorded in `parse_jobs`, `status` moved to `in_progress`) — that's
   Alpha's side working correctly up to the boundary.

**Known frontend gap, found while checking this:** `DealStatusPayload`
(`src/shared/dealsStatus.ts`) doesn't have a `jobComments` field yet, so
even once a run actually completes, findings/comments won't render — the
type and any UI for it still need adding on that side. Separate from the
stall above.

---

## Tier 3 — proving Alpha's own worker/orchestration through a *real* SAQ dispatch, without waiting on Services

Tier 1 calls task functions directly; Tier 2 stalls at the queue boundary.
This tier proves the actual `start_deal_analysis` → `start_deal_verification`
chain runs correctly when a **real** SAQ worker picks up a **real** job
from Valkey — using a throwaway local stub for `process_document`, never
committed, purely to unblock local testing before `Simpero_Gov_AI_Services`
has it.

**1. Add a temporary stub function** (do not commit) somewhere the worker
   loads, e.g. a scratch file imported by `app/jobs/tasks/__init__.py`
   only in your local checkout:

```python
# app/jobs/tasks/_local_stub_process_document.py  (temporary, gitignored/uncommitted)
async def process_document(ctx, *, spaces_key, entity, known_sha256s=None, audit=True):
    # Fakes a successful parse+extract+audit -- swap the envelope for
    # something that matches contracts/claims.schema.json if you want the
    # ingest step to have real claims to chew on.
    return {"status": "parsed", "bucket": "local-stub", "key": f"stub/{spaces_key}.json", "count": 0}
```

Temporarily add it to a **separate** `Queue` bound to the `"parse"` queue
name (the actual `Simpero_Gov_AI_Services` worker's queue) — Alpha's own
`app.jobs.tasks.__init__.functions` list is for the `"simpero"` queue only
and won't dispatch this. Simplest local approach: a second, throwaway SAQ
worker process pointed at the same Valkey, registering only this stub
under `"process_document"`:

```python
# scratch/stub_parse_worker.py (temporary)
from saq import Queue
queue = Queue.from_url("redis://localhost:6381", name="parse")
settings = {"queue": queue, "functions": [process_document], "concurrency": 1}
```

```bash
uv run saq scratch.stub_parse_worker.settings
```

**2. Run the real flow** (Tier 2's steps, or a direct API call via pytest's
   `TestClient`/a real Clerk token) with this stub worker running alongside
   Alpha's own worker container. The `"process_document"` job now gets
   picked up for real, returns the stub result, and Alpha's actual
   polling/outcome/chaining logic (`_apply_outcome`, the terminal branch
   that creates the `verification` run, `start_deal_verification`'s ingest
   loop) runs against a **real** queue round trip instead of a mock.

**3. Watch it progress**: `analysis_run` for the deal should walk
   `job_name="parsing"` → `successful` → a new `job_name="verification"`
   row → `successful` (with the stub's `count: 0`, verification will hit
   the "no claims to reconcile" path harmlessly). `GET /status` should
   advance through `"parsing"` → `"pass2"` → `"governance"` for real.

**Delete the stub before touching anything real** — it exists only to
exercise Alpha's own dispatch path, not to stand in for a real
implementation anywhere near production.

---

## Quick diagnostic: is a job actually landing in Valkey?

If a run seems stuck and you want to confirm whether Alpha enqueued
correctly (vs. Services not consuming — the expected-today state) or
something on Alpha's side silently didn't enqueue at all:

```bash
docker compose -f docker-compose.dev.yml exec valkey valkey-cli KEYS '*process_document*'
```

or, from Python, using SAQ directly against the same Valkey URL:

```python
from saq import Queue
import asyncio
q = Queue.from_url("redis://localhost:6381", name="parse")
job = asyncio.run(q.job("<job_key from analysis_run.parse_jobs>"))
print(job.status, job.result)
```

`job.status == "queued"` with no `result` confirms Alpha enqueued
correctly and nothing has consumed it yet — exactly the state Tier 2 stalls
in today.
