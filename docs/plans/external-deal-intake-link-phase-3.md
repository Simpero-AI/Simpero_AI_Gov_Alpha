# External Deal Intake Link — Implementation Brief (Phase P3, Alpha repo)

**Audience: two local Claude Code CLI sessions running against checkouts of `Simpero_AI_Gov_Alpha`** — one driven by Vansh, one by Suraj. This is one shared document because P3's tickets interleave between the two of you (see section 0.3) — read the whole thing even for tickets you don't personally own, since your branches will sit on top of each other's.

**Author: a Cowork planning session, verified against the live GitHub state of `Simpero_AI_Gov_Alpha` on 2026-08-26.** This is the third brief in this series (after the P1 brief for the RLS foundation and the Web brief for P4/P5) — it covers **P3: the org-side link-management routes and the public (unauthenticated) intake routes**, the layer that actually uses the P1 foundation and P2 question set to do the feature's real work.

---

## 0. Operating rules (read first — this phase has a real blocker P1/P2 didn't)

### 0.1 Current repo state, verified 2026-08-26 (not from the original plan — from the live API)

**Nothing in this feature has merged to `staging` yet.** Every P1, P2, and P3 ticket exists only as an open, unmerged pull request:

| Tickets | PRs | State |
|---|---|---|
| P1-00 → P1-05 (all 9 P1 tickets) | #114–#123 | Open, unmerged, **stacked on each other** in dependency order (each PR's base is the previous PR's head branch) |
| P2-01 (`deal_intake_questions` table) | #107 | Open, unmerged, base `staging` |
| P2-02 (admin CRUD router) | #108 | Open, unmerged, base = P2-01's branch |
| P2-03 (product-side read) | #110 | Open, unmerged, base = P2-01's branch — **a sibling of P2-02, not stacked on it** |
| P3-04 (`GET /deals/{id}/documents`) | #109 | Open, unmerged, base `staging` directly — **independent of P1/P2 entirely** |

The P1 stack, confirmed by walking each PR's base/head: `staging → p1-00 → p1-01 → p1-02 → p1-07 → p1-03 → p1-04 → p1-06 → p1-08 → p1-09 → p1-05`. The tip of that stack — the `p1-05-cross-tenant-negative-suite` branch (PR #123's head) — is the one branch that contains **all** of P1 merged together. That is the base P3 needs.

P2 is **not** a clean stack: #108 (P2-02, admin CRUD) and #110 (P2-03, product read) both branch independently off P2-01's branch, not off each other — this is the same add/add duplication I flagged in the PR #110 code review (both PRs independently create `app/repo/DealIntakeQuestionRepo.py`; the methods happen to be byte-identical, but git will still see it as a whole-file conflict when either merges second).

### 0.2 [FLAG — confirm before either of you starts] The branch-basing question

P3-01's own dependency list is `P1-01, P1-05, P2-03` — i.e. it needs the **full** P1 stack (P1-05 is P1's last ticket, "P1 is done when P1-05 is green") plus P2-03's product-read endpoint. Since none of that is merged to `staging`, you cannot just `git checkout staging && git checkout -b p3-01-...` — you'd be missing the `dd_public` role, both public dependencies, both keyhole policies, and `GET /api/intake-questions` entirely.

**I'm not deciding this for you — pick one before either session starts writing code, and record the choice in the status file (section 4):**

- **Option A (recommended): build a local integration branch.** `git checkout -b p3-base p1-05-cross-tenant-negative-suite && git merge surajk86808/p2-03-intake-questions-product-read` (fetch both branches first — they're on `origin`, not local). Branch all P3 work off `p3-base`. This is the only way to get real, running code for both dependencies before any of the underlying PRs are reviewed/merged — the alternative is blocking P3 entirely on someone reviewing and merging 12 open PRs first, which nobody has done in the two days they've been open.
- **Option B: wait for P1 and P2-03 to actually merge to `staging`, then branch normally.** Cleaner, but there's no signal in this repo that a merge is imminent — worth a direct check with whoever owns merging before assuming Option A is even necessary.

Whichever you pick, **P2-02 (#108, the admin CRUD router) is not a P3 dependency** — no P3 ticket lists it, and P3-01 only *reads* the question set (via P2-03), it doesn't need a way to *write* questions. But without P2-02 merged into your base, there is no API to seed real `deal_intake_questions` rows for testing — P3-01's tests will need to insert rows directly via the ORM/fixtures rather than through the admin endpoint. Not a blocker, just note it in the status file so nobody wonders later why P3-01's tests don't exercise the admin router.

**P3-04 (#109) does not need to be in your base branch at all** — no P3-0X ticket depends on it (it's a P5 dependency, not a P3 one), and it's based directly on `staging`, unrelated to the P1/P2 branches. Leave it out of `p3-base`; merge conflicts from an unrelated branch aren't worth inviting.

### 0.3 Ownership and why this can't just be split into two independent workstreams

Per `build_tickets.py`'s `OWNERS` dict:

| Owner | Tickets |
|---|---|
| **Vansh** | P3-01, P3-07, P3-11, P3-12, P3-13, P3-14 |
| **Suraj** | P3-02, P3-03, P3-05, P3-06, P3-08, P3-09, P3-10, P3-15 (P3-06, P3-09, P3-10, P3-15 are flagged `review=True` in the OWNERS dict — treat those four as needing a second pair of eyes before merge, on top of normal review) |

This looks like a clean 6/8 split until you trace the actual dependency graph (from each ticket's own "Depends on" column, not from the owner list) — and the graph crosses ownership repeatedly:

```
Wave 0 (branch off p3-base directly, no P3 dependencies):
  P3-01 (Vansh, needs P1-01/P1-05/P2-03)
  P3-05 (Suraj, needs P1-02)
  P3-07 (Vansh, needs P1-04)
  P3-10 (Suraj, needs P1-04)

Wave 1 (branch off the wave-0 ticket they depend on):
  P3-02 (Suraj) ← P3-01 (Vansh)
  P3-03 (Suraj) ← P3-01 (Vansh)
  P3-06 (Suraj) ← P3-01 (Vansh)
  P3-14 (Vansh) ← P3-01 (Vansh)
  P3-08 (Suraj) ← P3-07 (Vansh)
  P3-12 (Vansh) ← P3-07 (Vansh)
  P3-15 (Suraj) ← P3-10 (Suraj)

Wave 2:
  P3-09 (Suraj) ← P3-08 (Suraj)

Wave 3:
  P3-11 (Vansh) ← P3-09 (Suraj), P3-10 (Suraj)

Wave 4 (last — gates nothing, but everything gates it):
  P3-13 (Vansh) ← P3-07, P3-08, P3-09, P3-10, P3-11
```

Four of Suraj's wave-1 tickets (P3-02/03/06) branch off Vansh's P3-01, and one (P3-08) branches off Vansh's P3-07. Two of Vansh's tickets — P3-11 and P3-13 — can't start until Suraj's chains land. **This means: Vansh's session should build P3-01 and P3-07 first (both wave 0, no cross-dependency between them, can be done in either order or in parallel on two branches), push both branches, then hand off** — Suraj's session bases its wave-1 work on whichever of those two branches each ticket actually depends on (per the graph above, not off `p3-base` directly). P3-13, the 404-only contract audit, is deliberately last — it audits the other five routes, so it can't be written correctly before they exist.

Practically: **run this as a sequence of pushes and pulls between two local sessions, not two sessions working the whole list in isolation.** After each ticket lands on a branch, the owner of the *next* ticket in the graph needs to pull that branch before starting. The status tracking file (section 4) is where you coordinate this — mark a ticket's branch name and push status as soon as it's up, so the other person knows when they're unblocked.

### 0.4 Standing rules (same as the P1 and Web briefs)

1. **Scope for this pass: P3 only** (P3-01 through P3-15, excluding P3-04 which is already built on #109). Do not start P4/P5 work here even if a P3 ticket looks like it unblocks one — the Web brief covers that separately, and it has its own four-track tagging scheme for exactly this reason.
2. **One ticket at a time.** After each ticket, verify its acceptance criteria against real Postgres (same reasoning as P1 — RLS and `current_setting()` behavior cannot be exercised against SQLite). Do not move to the next ticket, or hand a branch off to the other owner, until acceptance criteria pass.
3. **Update the status file after every ticket**, not just at the end — see section 4. Given the cross-owner handoffs in 0.3, this file is now also the coordination mechanism between two people, not just a progress log for one.
4. **Do not deviate without flagging.** If the real code doesn't match what this brief describes (a P1 function signature that changed after review, a P2-03 response shape that's different from what's documented here), stop, write it down in the status file's "Flagged" section, and surface it in your summary — don't silently adapt.
5. **The 404-only contract is not optional and not "clean up later."** Every public route (P3-07 through P3-11) must return byte-identical 404s across every failure mode from the moment each route is written — P3-13 audits this at the end, it doesn't introduce it. Section 2.2 below has the exact rule.
6. **Every state-changing action gets exactly one `HumanAuditRepo` row, with the exact `event_type` string given in that ticket's description** — not a paraphrase, not a similar-sounding string invented in the moment. Section 2.3 collects the full list in one place so you can cross-check spelling.
7. **Commit as you go**, one commit per ticket, referencing the ticket ID, matching the P1/P2 PRs' own convention (visible in their titles and branch names).

---

## 1. Feature recap (context — full detail is in the P1 brief)

P3 is where the feature actually does its job: an org user generates a link (P3-01), the wizard polls its status (P3-02) or revokes it (P3-03), the external recipient proves their email (P3-07), answers questions (P3-08/09) and uploads documents (P3-10/15) without ever logging in, then explicitly submits (P3-11) — after which the org side can read the answers (P3-05), the pipeline grid reflects the deal's intake status (P3-06), and analysis is blocked from starting while a link is still pending (P3-14). P3-13 is the security audit that ties the whole public surface together.

Everything here runs on top of the RLS foundation P1 built: `dd_public`'s grant matrix, the two keyhole policies, and the two public dependency functions (`get_public_link_db`, `get_public_session_db`). If you haven't read the P1 brief, read section 4 of it (`external-deal-intake-link-implementation-brief.md`) before starting — this document summarizes only what P3 needs directly, in section 2 below.

---

## 2. Architecture recap — what P3 actually calls

### 2.1 The two public dependencies, and which P3 routes use which

```python
# app/core/public_dependencies.py — built in P1-04/P1-06, do not modify its
# contract here. P3 routes only ever depend on ONE of these each — never
# get_db, never both on the same route.

async def get_public_link_db(token: str) -> AsyncGenerator[AsyncSession, None]:
    # Used by exactly one route: P3-07 (POST /session). Everything after
    # /session uses get_public_session_db instead — the raw link token is
    # spent at /session and never sent again.
    ...

async def get_public_session_db(session_token: str) -> AsyncGenerator[AsyncSession, None]:
    # Used by P3-08, P3-09, P3-10, P3-11 — everything after /session.
    ...
```

Both dependencies yield `(session, link)` — the vouched-for `deal_intake_link` row is already available to every handler that needs `link.deal_id`, `link.org_id`, `link.recipient_email`, or `link.questions_snapshot`; there is never a reason for a P3 public route to re-query for the link by hand.

`app.intake_deal_id` is already set as a GUC by both dependencies (P1 section 4.4a) — this is what scopes `data_source` reads/writes (P3-10) to exactly one deal within the org, and it's why `deal_id` for uploads must come off the session, never the request body (see 2.2 below).

### 2.2 The 404-only failure contract (binding from P3-07 onward, audited by P3-13)

Every public route's failure path — bad token, expired, revoked, already submitted, wrong email, non-existent link — returns the **same 404 body**, no distinguishing 403, no message text a client could use to tell "wrong email" apart from "link revoked." P1-04/P1-06 already `raise HTTPException(404, ...)` on a missing link; every P3 handler you write on top of them must preserve that, and must not introduce a second, more specific error path (e.g. "don't add a 403 for 'email doesn't match' — that turns the endpoint into an oracle for confirming who a link was sent to").

### 2.3 The audit trail — every `event_type` string P3 writes, in one place

| Ticket | `event_type` | `actor_email` | Written on |
|---|---|---|---|
| P3-01 | `intake_link_generated` | NULL (org user captured via `created_by_user_id`) | normal link creation |
| P3-01 | `intake_link_reissued` | NULL | link creation on the branch where a stale pending row is lazily expired first |
| P3-03 | `intake_link_revoked` | NULL | org user revokes |
| P3-07 | `intake_email_attempt_succeeded` | the email that was tried | email match |
| P3-07 | `intake_email_attempt_failed` | the email that was tried | email mismatch |
| P3-10 | `intake_document_uploaded` | the verified session email | each successful `/complete` call, one row per document |
| P3-11 | `intake_submitted` | the verified session email | successful submit (also stamps `ip_address`, `user_agent`) |

These exact strings came from PO review (Q4 — "exact event list") and are quoted verbatim from `build_tickets.py`. Don't invent variants (`intake_link_created` instead of `intake_link_generated`, etc.) — anything reading this audit trail later will match on these literal strings.

### 2.4 The shared "effective status" helper — build this once, in P3-01

Four tickets (P3-01, P3-02, P3-06, P3-14) all need to compute the same thing: a link's status as-stored, except a `pending` row past `expires_at` reads as `expired`. **P3-01 is where this must be introduced as an importable function** (not inlined) — P3-02's own acceptance criteria explicitly says "the computed effective status (see the shared helper introduced in P3-01)," and P3-06/P3-14 both reference it the same way. If you build P3-01 without factoring this out into something the other three tickets can import, you'll end up with four slightly-different reimplementations, which is exactly the kind of drift this feature's RLS foundation was built to prevent at the database layer — don't reintroduce it at the application layer.

Only P3-01 performs the actual `UPDATE ... SET status = 'expired'` write (lazily, right before a reissue) — P3-02/P3-06/P3-14 only ever *read* the computed value, never write it.

---

## 3. P3 tickets, in dependency-wave order

Each entry gives: owner, branch-basing instruction (per section 0.3's graph), the ticket text verbatim from `build_tickets.py`, and its acceptance criteria.

### Wave 0 — branch off `p3-base` directly

#### P3-01 — `POST /api/deals/{deal_id}/intake-link`
**Owner: Vansh. Priority P0. Depends on: P1-01, P1-05, P2-03. Branch off `p3-base`.**

Generates the link: `secrets.token_urlsafe(32)`, stores only its SHA-256, captures `questions_snapshot` from the active set, sets `expires_at = now() + 7 days`. Before inserting, lazily expires any existing row for this `deal_id`: if it is `pending` but `expires_at <= now()`, UPDATE it to `expired` first (this is the only place that ever writes `status = 'expired'` — there is no scheduled job for it, added after PO review flagged that nothing wrote it and the partial unique index would otherwise block a reissue). Returns the raw token exactly once — never stored, never retrievable again. 409 if an `analysis_run` already exists for the deal, or if a still-live pending link already exists (partial unique index surfaces as `IntegrityError` → 409). `HumanAuditRepo` row on success: `event_type = intake_link_generated` normally, or `intake_link_reissued` on the branch where a stale pending row was lazily expired first — `actor_email` NULL, the org user is already captured by `created_by_user_id` on the link row (Q4/PO review, exact event list).

**Acceptance criteria:** Raw token appears only in this one response body, confirmed by grepping logs after a test run. A second call while a live pending link exists returns 409, not a second row. A second call where the only existing row is `pending` but past its `expires_at` succeeds, flips that row to `expired`, and inserts the new one — the reissue case. A deal with an `analysis_run` returns 409.

*Sourcing: Q3, Q4, Q11 (reissue must not be blocked by a stale pending row), Q15 — the generate-time enforcement of the TTL/one-link/post-analysis decisions.*

**Build note:** this ticket is also where the shared effective-status helper (section 2.4) must be introduced as an importable function.

#### P3-05 — `GET /api/deals/{deal_id}/intake-response`
**Owner: Suraj. Priority P1. Depends on: P1-02. Branch off `p3-base`.**

Returns the submitted answers for the org-side reader: `id`, `dealId`, `respondentEmail`, `submittedAt`, `answers[]` (`questionKey`, `prompt`, `answer`, `answered`) — the exact shape documented in "Stored shapes" (P1 brief, section 2.4). 404 if nothing has been submitted yet.

**Acceptance criteria:** Response matches the documented wire shape exactly, camelCased via `CamelModel`.

*Sourcing: Q12 — the human-read surface for Step 3 and the deal detail page.*

#### P3-07 — `POST /api/public/intake/{token}/session`
**Owner: Vansh. Priority P0. Depends on: P1-04. Branch off `p3-base`.**

Body `{email}`. Matches against `recipient_email` case-insensitively via `get_public_link_db` (not `get_public_session_db` — this is the one route that runs before a session exists). On match, issues a short-lived (30 min) intake session token via `encode_intake_session_jwt` — the encode counterpart to P1-06's `decode_intake_session_jwt` — scoped to `{link_id, deal_id, email}`, signed with its own key (never Clerk's), carrying an intake-specific audience claim. On mismatch, increments `failed_attempts` and returns the same generic 404 as every other failure mode. `HumanAuditRepo` row on both outcomes — `event_type = intake_email_attempt_succeeded` or `intake_email_attempt_failed`, `actor_email` = the email that was tried either way. Per the PO, this pair matters most: it's the evidence trail if a link gets forwarded somewhere it shouldn't have gone (Q4/PO review, exact event list).

**Acceptance criteria:** 5 consecutive mismatches lock the link (see P3-12). The session token is rejected by `decode_clerk_jwt` and vice versa — confirmed by a test that feeds each decoder the other's token. Both a match and a mismatch produce exactly one audit row each, with the tried email captured on the failure row too.

*Sourcing: Q1 (shaped so an OTP step can be inserted here later without reshaping anything downstream); Q4/PO review (exact event list).*

**Note if P1-06 hasn't landed a real `encode_intake_session_jwt` yet:** the P1 brief (section 4.3) already anticipated this — P1-06's own acceptance criteria required at minimum a testable decode/encode pair. Confirm the real encode function exists and matches the claims shape described above before building this ticket on top of a stub.

#### P3-10 — Public: presigned-url + complete uploads
**Owner: Suraj. Priority P0. Depends on: P1-04. Branch off `p3-base`.**

`POST /api/public/intake/uploads/presigned-url` and `.../{id}/complete` — thin handlers over the deal, with `deal_id` read off the intake session (never the request body) and business logic reused from `app/services/uploads/spaces.py` (`build_object_key`, `presign_put`, `head_object`) and `DataSourceRepo`. Deliberately a separate pair of routes from `/api/uploads/*`, not a second auth branch on the existing ones. Enforce the 20-file-per-link ceiling and the existing 10 MB / extension allowlist. `HumanAuditRepo` row from `/complete`, one per document: `event_type = intake_document_uploaded`, `actor_email` = the verified session email (Q4/PO review, exact event list).

**Acceptance criteria:** A 21st upload attempt for one link is rejected before a presigned URL is issued. The resulting `data_source` row is byte-for-byte identical in shape to one created by the authenticated org-side path. Each successful `/complete` call produces exactly one `intake_document_uploaded` audit row.

*Sourcing: Q9, "Why not the alternatives" in the plan's RLS section.*

**Important — this ticket actually depends on `get_public_session_db`, i.e. on P3-07 existing to issue that session in the first place**, even though the ticket's own "Depends on" column only lists P1-04. You can write and unit-test the route handlers against P1-04 alone, but you cannot exercise the real end-to-end flow (or hand this off to P3-15/P3-11) until a P3-07 session actually exists. Coordinate with Vansh's P3-07 branch before calling this "done" in the status file.

### Wave 1 — branch off the wave-0 ticket listed as "Depends on"

#### P3-02 — `GET /api/deals/{deal_id}/intake-link`
**Owner: Suraj. Priority P1. Depends on: P3-01. Branch off Vansh's `p3-01-...` branch (pull it first).**

Status endpoint for the wizard's Step 2 waiting panel: `status`, `recipient_email`, `expires_at`, `submitted_at`. Never returns the token or its hash. Returns the computed effective status (the shared helper introduced in P3-01), not the raw stored column — a row still stored as `pending` but past `expires_at` reads as `expired` here even though P3-01's lazy write has not necessarily run yet.

**Acceptance criteria:** Response body contains no `token_hash` field under any status. Calling this immediately after `expires_at` has passed, before any generate call has run the lazy-expire write, still returns `status: 'expired'`, not `'pending'`.

*Sourcing: Q11/PO review — effective status must not lag the stored column.*

#### P3-03 — `DELETE /api/deals/{deal_id}/intake-link`
**Owner: Suraj. Priority P1. Depends on: P3-01. Branch off Vansh's `p3-01-...` branch.**

Revoke: flips status `pending → revoked` via the one legitimate UPDATE path. `HumanAuditRepo` row on every revoke: `event_type = intake_link_revoked`, `actor_email` NULL (Q4/PO review, exact event list).

**Acceptance criteria:** A revoked link's token immediately fails the keyhole policy (covered by P1-05's pattern, extended to this case).

#### P3-06 — Add `intakeStatus` to `GET /deals/pipeline`
**Owner: Suraj. Priority P1. Depends on: P3-01. Branch off Vansh's `p3-01-...` branch.**

Add `intakeStatus: 'none' | 'pending' | 'submitted'` to `LivePipelineRowResponse`, derived from the deal's most recent `deal_intake_link` row's effective status (the same shared helper as P3-02, not the raw stored column) — or `'none'` if there isn't a row. Powers the frontend's conditional grid routing.

**Acceptance criteria:** A deal with no link ever generated returns `'none'`. A deal whose link was revoked or expired also reads `'none'` — the grid falls back to normal analysis routing, it does not need a fourth state. A deal whose only link row is stored `pending` but past `expires_at` also reads `'none'`, not `'pending'` — the grid never routes to a waiting panel for a link that is functionally dead.

*Sourcing: F4, D3 — "only external-flow deals route to the wizard"; Q11/PO review (effective status).*

#### P3-14 — Analysis gate: block Start Analysis while an intake link is pending
**Owner: Vansh. Priority P0. Depends on: P3-01. Branch off your own `p3-01-...` branch.**

New guard clause in `start_analysis`, alongside the existing `analysis_repo.active_for_deal` check: 409 if the deal has an intake link whose effective status (the P3-02/P3-06 helper) is `'pending'`. Added after PO review flagged that Q15 only blocks generating a NEW link once analysis exists — nothing previously stopped a link issued before analysis started from letting documents keep arriving through the public upload routes mid-pipeline, which nothing downstream (the job chain, screening, the AI-services parse lane) expects.

**Acceptance criteria:** Calling `POST /deals/{id}/analysis` while the deal has a pending intake link returns 409, even with a verified `data_source` already present. Once that link is submitted, revoked, or expired, the same call proceeds exactly as it does today.

*Sourcing: Q5/PO review — the mirror of Q15, closing the other direction of the race.*

**Note:** this ticket depends on the same shared effective-status helper as P3-02/P3-06 — since those are Suraj's branches, you (Vansh) will need the helper function itself (from your own P3-01 branch), not their endpoints. No actual cross-branch dependency here despite the naming overlap — just don't reimplement the helper a second time.

#### P3-08 — Public: `GET /api/public/intake/questions`
**Owner: Suraj. Priority P1. Depends on: P3-07. Branch off Vansh's `p3-07-...` branch (pull it first).**

Returns `questions_snapshot` from the link row (via the intake session, not the raw token) plus the org's display name. Nothing else — no deal name, no deal size, no GP/source, no other party's answers.

**Acceptance criteria:** Response body contains no field derivable from the `deals` table beyond what is explicitly allowed.

*Sourcing: Q6 — org name only, no deal name.*

#### P3-12 — Rate limiting on the public intake surface (Valkey-backed middleware)
**Owner: Vansh. Priority P0. Depends on: P3-07. Branch off your own `p3-07-...` branch.**

Two layers: per-link `failed_attempts` lockout (already tracked on the row by P3-07) requiring the org user to reissue after ~5 mismatches; per-IP (and, on session-authenticated routes, per-`link_id`) request throttling on the `/api/public/*` prefix via a Valkey-backed ASGI middleware, registered in `app/main.py` the same way `CORSMiddleware` already is. Rescoped after PO review flagged the original plan — Caddy's `rate_limit` handler — as more than a config change: it ships in the third-party `caddy-ratelimit` module, not Caddy core, and the stock `caddy:2-alpine` image this stack's Dockerfile/`docker-compose.prod.yml` actually run does not include it, so using it would mean standing up and maintaining a custom `xcaddy` build. Valkey is already live infrastructure (the SAQ job queue depends on it), so the middleware route adds no new deployment artifact.

**Acceptance criteria:** An automated script hammering `/session` with random emails gets throttled at the app layer before it exhausts the per-link attempt budget, with no change to the Caddy image or its build. The counter survives across app instances (shared via Valkey, not per-process memory).

*Sourcing: F8 — there is no rate limiting anywhere in the stack today; F8/PO review — Caddy `rate_limit` needs a custom `xcaddy` image, so it is rescoped to app-level middleware.*

#### P3-15 — Signed content-length ceiling on `presign_put` (F9)
**Owner: Suraj. Priority P1. Depends on: P3-10. Branch off your own `p3-10-...` branch.**

`presign_put(key, ttl_seconds)` currently signs `Bucket` + `Key` only, so the 10 MB cap is enforced after the object is already stored (in `ingest_data_source`'s `stream_and_hash`). Add an explicit content-length-range condition to the presigned PUT so Spaces itself refuses an oversized body before any bytes land. Applies to both `build_object_key` call sites, org and public — the public path is where an unidentified caller amplifying it actually matters, but the fix is one function.

**Acceptance criteria:** A PUT exceeding 10 MB against a freshly presigned URL is rejected by Spaces with no object written, confirmed by a `head_object` call after the attempt returns nothing. A PUT at or under the limit succeeds unchanged.

*Sourcing: F9 — flagged during the anonymous-uploader audit, 2026-08-25.*

**Note:** this fixes `presign_put` itself, which is shared by the existing authenticated org-side upload path too — verify the org-side upload flow's own tests still pass after this change; this is the one P3 ticket that touches code outside the intake feature's own files.

### Wave 2

#### P3-09 — Public: `POST /api/public/intake/answers`
**Owner: Suraj. Priority P0. Depends on: P3-08. Branch off your own `p3-08-...` branch.**

Validates `SubmitAnswersRequest` against the link's own snapshot: every `question_key` must exist in it, no duplicates, no invented keys, every required question non-blank, each answer ≤ 4000 characters (422, never silently truncated). `prompt` and `answered` are filled server-side from the snapshot, never accepted from the client. Editable by repeated calls before Submit; the response is written only at Submit (see P3-11).

**Acceptance criteria:** A key outside the snapshot is rejected with 422, not silently dropped or accepted. An answer over 4000 chars is rejected, not truncated.

*Sourcing: Q8 (editable pre-submit), the "stored shapes" validation block.*

### Wave 3

#### P3-11 — Public: `POST /api/public/intake/submit`
**Owner: Vansh. Priority P0. Depends on: P3-09, P3-10. Branch off a merge of Suraj's `p3-09-...` and `p3-10-...` branches (both, not either) — pull both first.**

The explicit finish (Q2 in the decisions table / D2): writes the `deal_intake_response` row from the currently-held answers, requires ≥ 1 verified-or-pending `data_source` for the deal, flips the link to `submitted` via the one-way trigger, stamps `submitted_at`. `HumanAuditRepo` row: `event_type = intake_submitted`, `actor_id=null`, `actor_email` = the verified session email, `ip_address`, `user_agent` (Q4/PO review, exact event list).

**Acceptance criteria:** A second call after a successful submit fails closed (link no longer visible under the keyhole policy's `status='pending'` clause) rather than duplicating the response row. A submit attempt with zero uploaded documents is rejected.

*Sourcing: D2, Q9 (≥1 document required).*

### Wave 4 — last, gates the phase close

#### P3-13 — 404-only failure contract across every public route
**Owner: Vansh. Priority P0. Depends on: P3-07, P3-08, P3-09, P3-10, P3-11. Branch off a merge of all five: your own `p3-07-...`/`p3-12-...`, Suraj's `p3-08-...`/`p3-09-...`/`p3-10-...`/`p3-15-...`. This is the integration point for the whole public surface — expect this branch to need the most merge attention of anything in P3.**

Audit every `public_intake.py` handler: bad token, expired, revoked, already submitted, and wrong email must all return the identical 404 body. No distinct 403, no distinct message text that would let a client distinguish "wrong email" from "link revoked."

**Acceptance criteria:** A test asserts byte-identical response bodies across all five failure modes.

*Sourcing: prevents the endpoint becoming an oracle for enumerating links or confirming recipient addresses.*

**P3 is done when P3-13 is green** — mirroring how the P1 brief treats P1-05 as the phase's own gate.

---

## 4. Status tracking file — create this now, in `docs/plans/external-deal-intake-link-p3-status.md`

This file is shared between both of you — it's the coordination mechanism for the cross-owner handoffs in section 0.3, not just a log. Update it the moment a branch is pushed, not only when a ticket is fully "done" — the other owner may be blocked waiting on that push.

```markdown
# External Deal Intake Link — P3 implementation status

Started: <date>
Implementing sessions: Vansh (local Claude Code CLI), Suraj (local Claude Code CLI)
Source spec: docs/plans/external-deal-intake-link-p3-implementation-brief.md
Base branch decision (section 0.2): <Option A / Option B — and if A, confirm p3-base was created from p1-05-cross-tenant-negative-suite + surajk86808/p2-03-intake-questions-product-read>

## Tickets

| Ticket | Owner | Status | Branch | Based on | Pushed? | Tested against | Notes |
|---|---|---|---|---|---|---|---|
| P3-01 | Vansh | | | p3-base | | | |
| P3-05 | Suraj | | | p3-base | | | |
| P3-07 | Vansh | | | p3-base | | | |
| P3-10 | Suraj | | | p3-base | | | |
| P3-02 | Suraj | | | P3-01 branch | | | |
| P3-03 | Suraj | | | P3-01 branch | | | |
| P3-06 | Suraj | | | P3-01 branch | | | |
| P3-14 | Vansh | | | P3-01 branch | | | |
| P3-08 | Suraj | | | P3-07 branch | | | |
| P3-12 | Vansh | | | P3-07 branch | | | |
| P3-15 | Suraj | | | P3-10 branch | | | |
| P3-09 | Suraj | | | P3-08 branch | | | |
| P3-11 | Vansh | | | P3-09 + P3-10 branches | | | |
| P3-13 | Vansh | | | P3-07/08/09/10/11/15 (all) | | | |

## Flagged (things that deviated from the brief, or decisions punted back to Vansh)

- <e.g. P1-06's encode_intake_session_jwt shape, if it turned out different from the P1 brief's description>
- <the P2-02-not-merged testing gap noted in section 0.2, and how you worked around it>

## Cross-owner handoff log (who pulled what, when)

- <e.g. "2026-08-27: Suraj pulled Vansh's p3-01-intake-link-generate branch to start P3-02">
```

---

## 5. First prompts

### For Vansh's session

> Read `docs/plans/external-deal-intake-link-p3-implementation-brief.md` in full before doing anything else — including the sections describing Suraj's tickets, since your P3-14/P3-12/P3-11/P3-13 branches sit on top of work Suraj is building in parallel. Start by resolving section 0.2 (the branch-basing decision) if it isn't already resolved — check `docs/plans/external-deal-intake-link-p3-status.md` first in case Suraj's session already did. Then build P3-01 and P3-07 (your two wave-0 tickets, independent of each other), push both branches, and update the status file the moment each is pushed — Suraj's session is blocked on both. After that, work P3-14 and P3-12 (each off your own wave-0 branch), then wait for Suraj's P3-09 and P3-10 to land before starting P3-11, then wait for everything before starting P3-13. Verify every ticket's acceptance criteria against real Postgres before marking it done. Flag anything that doesn't match this brief instead of silently adapting around it — see operating rule 4.

### For Suraj's session

> Read `docs/plans/external-deal-intake-link-p3-implementation-brief.md` in full before doing anything else — including the sections describing Vansh's tickets, since several of your branches (P3-02/03/06, P3-08, and later P3-09) are based on branches Vansh is pushing, not on the shared `p3-base` directly. Check `docs/plans/external-deal-intake-link-p3-status.md` before starting each ticket to confirm the branch you need to base it on has actually been pushed — if it hasn't, work on P3-05 or P3-10 instead (your two wave-0 tickets, independent of Vansh's work) while you wait. Verify every ticket's acceptance criteria against real Postgres before marking it done, and update the status file the moment each branch is pushed, since P3-11 and P3-13 (Vansh's) are waiting on your P3-09/P3-10/P3-15. Flag anything that doesn't match this brief instead of silently adapting around it.

---

## 6. Where this brief's content came from

Ticket text (descriptions, acceptance criteria, sourcing notes) is quoted verbatim from `build_tickets.py` (P3-01 through P3-15). The architecture recap in section 2 is drawn from the same plan document the P1 brief was built from, condensed to what P3 actually calls. Everything in sections 0.1, 0.2, and 0.3 — the current PR/branch state, the branch-basing recommendation, and the cross-owner dependency graph — is new analysis for this brief, built by walking the live GitHub API state of all P1/P2/P3 pull requests on 2026-08-26 and cross-referencing each ticket's own "Depends on" column against the `OWNERS` dict; none of it existed in the original plan, since the original plan predates any of this work being split across two people's branches.