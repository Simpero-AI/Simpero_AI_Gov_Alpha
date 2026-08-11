# Two gaps in `Simpero_AI_Gov_Web` — handoff to that repo's implementor

> **This is a handoff document**, written from a `Simpero_AI_Gov_Alpha`
> session for whoever implements frontend changes in `Simpero_AI_Gov_Web`.
> Hand this file (or paste it) to that repo's owner/session to act on —
> per that repo's own `CLAUDE.md`, a `Simpero_AI_Gov_Web` session is
> forbidden from implementing backend changes, and this doc requires none;
> everything here is frontend-only.
>
> Found while checking whether that repo's already-in-progress,
> **uncommitted** rewire of the deal-creation/analysis flow
> (`src/api/deals.ts`, `src/pages/NewDealWizard.tsx`,
> `src/pages/newDealWizard/newDealWizardReducer.ts`, etc.) is streamlined
> with Alpha's current backend contract. Short version: it mostly already
> is — `createDeal`/`startDealAnalysis` correctly call `POST /deals` /
> `POST /deals/{dealId}/analysis`, `dealId` is correctly typed as a UUID
> string throughout, the 409 "already running" case is handled correctly
> against Alpha's real error text, and `pipelineSteps.ts`'s 9-phase list
> already matches Alpha's `pipeline_steps.py` exactly (no change needed
> there — it'll render `pass2`/`governance` correctly once the backend
> sends them, since that logic is already phase-name-driven, not
> hardcoded to specific phases). Two real gaps found on top of that
> already-good work, both small.

---

## Gap 1 — `DealStatusPayload` is missing `jobComments`

**Where:** `src/shared/dealsStatus.ts`

Alpha's `GET /deals/{dealId}/status` now returns an additional field,
`jobComments`, on `DealStatusResponse` — a frontend-facing findings summary,
`null` until a run reaches a terminal status (`successful`/`failed`), then
one entry per document:

```ts
type JobComment = {
  dataSourceId: string;
  fileName: string | null;
  status: string;   // e.g. "parsed", "rejected", "ingested", "verified"
  comment: string;  // human-readable — either the parser's own message, or
                     // this app's summary text (e.g. "2 claim(s) ingested,
                     // 1 same_fact edge...")
};
```

**Fix:**

```ts
// src/shared/dealsStatus.ts
export interface DealStatusPayload {
  jobStatus: "queued" | "processing" | "complete" | "error" | "no_job";
  currentPhase: string | null;
  steps: PipelineStepWithStatus[];
  phaseProgress?: { completed: number; total: number } | null;
  errorMessage?: string | null;
  /** Frontend-facing findings summary, one entry per document. Null until
   * the run reaches a terminal status. */
  jobComments?: JobComment[] | null;
}
```

(`src/api/_legacy/shared/dealsStatus.d.ts` is a separate, frozen legacy
type file — leave it alone unless something there is also actually
consumed at runtime; the real one is `src/shared/dealsStatus.ts`, per that
file's own docstring.)

**Where to actually render it:** `src/pages/DealAnalysis.tsx`, both places
that currently pass `steps`/`phaseProgress` into `AnalysisProgressView` —
the `jobStatus === "error"` branch (~line 1238) and the
`jobStatus === "queued" || "processing"` branch (~line 1253). `jobComments`
will be non-null exactly when there's something worth showing (the run
just finished a stage) — likely a new prop on `AnalysisProgressView` itself,
or a small findings list rendered alongside it. Not designed further here —
that's a real UI decision for whoever picks this up, not dictated by this
doc.

---

## Gap 2 — `e2e/analyse-async.spec.ts` tests a dead endpoint

**Where:** `e2e/analyse-async.spec.ts`

This test still posts to `/api/simpero/analyse?async=1` and asserts a
`{jobId, pollUrl, sessionId}` response shape with a `pollUrl` matching
`/api/simpero/analyse-job/` — the legacy tRPC-era contract. That endpoint
doesn't exist in the current FastAPI backend at all (confirmed — see
`Simpero_AI_Gov_Alpha`'s `app/main.py` router list). It's currently
harmless: gated behind `test.skip(!process.env.E2E_BACKEND_FIXTURES, ...)`,
so it never actually runs in CI as configured. But it's stale and
misleading to anyone reading it as documentation of the current contract.

**Fix, either:**
- **Delete it** — the real flow is already covered by
  `e2e/g31-new-deal-wizard.spec.ts` (already modified in the in-progress
  work found), which presumably exercises the real
  `POST /deals`/`POST /deals/{dealId}/analysis` path. Confirm that's true
  before deleting.
- **Or rewrite it** to target the real endpoints: `POST /deals/{dealId}/analysis`
  returns `202` with a `DealStatusPayload` body directly (no separate
  `pollUrl` — poll `GET /deals/{dealId}/status` on the same `dealId`
  instead). Needs a real `dealId` either way (the test already gates on
  `E2E_DATABASE_URL` for that reason) — same fixture-availability caveat
  as today, just against the real contract instead of the dead one.

Either is a small, contained change — this doc isn't prescribing which,
just flagging that the test is currently testing nothing real.

---

## Not a gap, confirmed correct — no action needed

- `src/api/deals.ts::createDeal`/`startDealAnalysis` — request/response
  shapes match Alpha's `CreateDealRequest`/`CreateDealResponse`/
  `StartAnalysisRequest`/`DealStatusResponse` exactly.
- `dealId` typed as `string` (UUID) throughout the wizard reducer and
  `NewDealWizard.tsx` — matches `Deal.id`.
- The 409 "already running" detection in `NewDealWizard.tsx::handleSubmit`
  (`err.message.toLowerCase().includes("already running")`) correctly
  matches Alpha's real detail text, `"Analysis is already running for this
  deal"`.
- `src/shared/pipelineSteps.ts`'s `PIPELINE_STEPS` — identical 9 phases,
  same order, same titles/details as Alpha's `app/services/pipeline_steps.py`.
  `computeStepStatuses` is phase-name-driven, so it needs no change for
  `pass2`/`governance` to render correctly once Alpha's backend reports
  them (already does, as of this session's rework).
- `src/hooks/useUploadDocument.ts` already has copy for `ocr_needed` — the
  SIM-350 signal is already accounted for on this side.

---

## Context for whoever picks this up

Both gaps were found while checking readiness for a still-in-progress
backend change (`Simpero_AI_Gov_Alpha` PR #81, not yet merged): a
combined parse+extract+verify pipeline that adds the `jobComments` field
and moves `currentPhase` through `pass2`/`governance` for a completed run.
Full backend context, if useful: `docs/plans/analysis-pipeline-stage-chaining.md`
and `docs/local-testing-guide.md` in `Simpero_AI_Gov_Alpha`. Neither gap
here blocks that backend work from merging — they're independent, and the
backend will send `jobComments`/the new phase values regardless of whether
this repo renders/tests them yet.
