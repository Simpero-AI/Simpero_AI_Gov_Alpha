# P3-09 — Public: `POST /api/public/intake/answers`

**Status:** done, verified against real Postgres, full suite green (1027/1027
on a fresh volume).
**Ticket:** P3-09 (`docs/plans/external-deal-intake-link-p3-status.md`,
`docs/plans/external-deal-intake-link-phase-3.md`, lines 268-275).
**Session:** 2026-08-28. Picked up by Vansh's session directly (normally
Suraj's ticket — Suraj's wave-0/1 tickets hadn't started; P3-09 was
building toward unblocking P3-11).

---

## What this is

`POST /api/public/intake/answers`, session-authenticated via
`get_public_session_db`, letting an external intake-link recipient save
draft answers across multiple calls before eventually submitting (P3-11,
separate ticket). Added to the existing `app/api/public_intake.py` router.

## The design decision this ticket needed before implementation

`deal_intake_response` (where the final submitted answers land) is
INSERT-only for `dd_public` — no SELECT, no UPDATE — so it can't hold
in-progress draft state. `deal_intake_link` had no column for this either.
Three options were weighed (architect pass, approved by Vansh before
implementation, per this session's "always get sign-off on architectural
decisions" rule):

- **Chosen: add `draft_answers JSONB NULL` to `deal_intake_link`**, in the
  same narrow-column-grant idiom the table already uses for
  `status`/`submitted_at`/`failed_attempts`/`last_attempt_at`.
- **Rejected: Valkey-backed draft state.** The P1 design's own invariant is
  that `dd_public`'s write surface is provable at the DB/RLS level ("touch
  exactly five tables, provably"). Draft answers are real tenant data
  pending validation, not ephemeral cache data (unlike P3-12's rate-limit
  counters) — routing them through Valkey moves part of that surface
  outside RLS and outside `tests/test_dd_public_grant_drift.py`'s coverage.
  Also has an unsolvable TTL mismatch: a fixed Valkey TTL vs. each link's
  own variable `expires_at`.
- **Rejected: stateless, P3-11 carries the answers in its own request
  body.** Reshapes a documented contract (P3-11 currently has no request
  body) to avoid persistence P3-09 needs regardless — P3-09's own "editable
  by repeated calls" behavior requires persisting *something* across calls;
  this option just relocates that persistence to the client, which is
  worse for an applicant resuming a form days later on a different device.

## What was built

- **Migration** `alembic/versions/2f7e83611f52_deal_intake_link_draft_answers.py`
  (`down_revision = 76a165315331`): adds `deal_intake_link.draft_answers`
  (nullable JSONB) and a **new, separate** `GRANT UPDATE (draft_answers) ON
  deal_intake_link TO dd_public` statement — deliberately does not modify
  `3d7b1f5a8c94`'s original narrow-grant statement, so that migration's
  diff stays legible as a pure addition. `dd_app` does NOT get
  `draft_answers` added to its own UPDATE grant — the org side has no
  legitimate write path to the external party's in-progress draft (it
  already gets default SELECT on the new column for free, harmless).
  `dd_public` needs no new SELECT grant either — it already holds
  whole-table SELECT (P1-01), so the new column is automatically visible.
- `app/models/deal_intake_link.py` — `draft_answers: Mapped[dict | None]`
  (JSONB).
- `app/repo/IntakeLinkRepo.py` — new `update_draft_answers(link_id,
  draft_answers) -> bool`. Uses `.returning(DealIntakeLink.id)` +
  `result.first() is not None` rather than raising on failure. **Verified
  finding, not assumed**: a stale call against an already-submitted/
  revoked/expired link doesn't hit `trg_deal_intake_link_one_way_status`
  at all (that trigger only fires on rows the UPDATE actually touches) —
  it's `dd_public`'s `intake_link_status_update` RLS policy
  (`b4f8e1c3a962`) that's the real gate: its `USING` clause requires
  `status = 'pending' AND expires_at > now()`, so a stale call simply
  matches **zero rows**, no exception, no trigger involvement. Confirmed
  by reading the policy definition directly, not just testing behavior.
  The route translates "zero rows" straight to the same 404 every other
  public-route failure returns.
- `app/api/public_intake.py` — new `POST /answers` route plus two small
  module-level helpers:
  - `_validate_answers(answers, lookup)`: per-call validation against
    **only the keys present in that call** — an unknown `question_key` or
    a duplicate within one request payload is 422; a `required` question
    answered blank in that call is 422; an answer over 4000 chars is 422.
    Never truncates or silently drops. This is deliberately *not*
    "every required question in the whole snapshot must be answered" —
    that reading would make partial/progressive saves (answer 2 of 8
    today, the rest tomorrow) fail every call but the last, which
    contradicts the ticket's own "editable by repeated calls" text.
  - `_seed_draft(snapshot_questions)`: synthesizes a full draft (every
    question at `answered=False, answer=""`) the first time a link is
    ever saved to (`link.draft_answers is None`).
  - The route itself: read-merge-write. Reads the current draft (or seeds
    one), overlays this call's validated entries (server-deriving `prompt`
    from the snapshot and `answered` from whether the trimmed answer is
    non-empty — never accepted from the client), writes the merged
    `{"schema_version": 1, "answers": [...]}` blob back via
    `update_draft_answers`. 404 if that returns `False`.
- `app/schemas/public_intake.py` — `AnswerInput` (client input:
  `question_key`, `answer` only — `prompt`/`answered` are not accepted
  fields at all, not just ignored-if-present), `SubmitAnswersRequest`,
  `DraftAnswerResponse`, `SubmitAnswersResponse` (echoes the full merged
  draft back — lets the frontend show current save state without a second
  GET, cheap since the merged dict is already in hand; not explicitly
  specified by the ticket text, a judgment call).
- `tests/test_dd_public_grant_drift.py` — two new expected-privilege rows
  for `draft_answers` (SELECT, UPDATE), same PR as the migration.
- `tests/test_public_answers_api.py` (new, 10 tests): unknown-key 422,
  duplicate-key 422, blank-required-question 422, over-length-answer 422,
  partial-save-merges-across-two-calls (the core "editable by repeated
  calls" behavior, tested explicitly), server-derived `prompt`/`answered`
  with client-supplied values for those fields ignored, non-`pending`-link
  404 (parametrized across `submitted`/`revoked`/`expired`), cross-org
  isolation.

## Verification

- `uv run pytest` (full suite, real Postgres, fresh dev volume) —
  **1027 passed, 2 skipped, 1 deselected** (the two known pre-existing
  environment artifacts already documented in this status doc's Flagged
  section).
- `uv run pyright` — 0 errors, 0 warnings, 0 informations. (Caught and
  fixed two `reportOptionalSubscript` errors in the test file during
  review — a test helper returning `dict | None` was subscripted in two
  places without a `None` check; added `assert draft is not None` at both
  call sites.)
- `alembic heads` — single head (`2f7e83611f52`) after the new migration.
- Manually traced `intake_link_status_update`'s policy definition
  (`b4f8e1c3a962`) to confirm the 404-on-stale-link claim above, rather
  than trusting the implementer's report at face value.

## Branch / PR note

This branch (`p3-09-public-answers`) is based on `p3-08-public-questions`
(P3-08's own commit, not `origin/staging`) since P3-09 genuinely depends on
P3-08's code (shares `app/api/public_intake.py`) and P3-08's own PR (#151)
isn't merged yet. Opened as a **stacked PR**, base `p3-08-public-questions`
— matches this project's established stacked-PR precedent (P3-12/P3-14 on
P3-07/P3-01). Per the already-documented CI gap (this status doc's Flagged
section), CI will not trigger on a PR whose base isn't `main`/`staging`
until #151 merges and this PR can be retargeted.

## Deviations / judgment calls worth flagging

- **Response shape** (echo the full merged draft) wasn't explicit in the
  ticket text — flagging in case a leaner ack-only response is preferred.
- **First-call seed behavior**: the very first `/answers` call for a link
  seeds a full draft covering every snapshot question (all initially
  unanswered), not just the keys sent in that first call. This means a
  `GET`-equivalent read of the draft (if one is ever added) would show the
  complete question set from the first save onward, not just what's been
  answered so far — matches the architect's explicit spec, flagging so
  it's a known, deliberate choice if it ever looks surprising later.
