# P3-08 — Public: `GET /api/public/intake/questions`

**Status:** done, verified against real Postgres, full suite green (1017/1017
on a fresh volume).
**Ticket:** P3-08 (`docs/plans/external-deal-intake-link-p3-status.md`,
`docs/plans/external-deal-intake-link-phase-3.md`, lines 237-244).
**Session:** 2026-08-28. Picked up by Vansh's session directly (normally
Suraj's ticket — needed to unblock the P3-09 → P3-11 chain). No
architectural decision required: fully spec-pinned, a single read-only
route reusing an established pattern.

---

## What this is

`GET /api/public/intake/questions`, session-authenticated via
`app/core/public_dependencies.py::get_public_session_db`. Returns the
link's frozen `questions_snapshot["questions"]` array plus the org's
display name — nothing else. Added to the existing `app/api/public_intake.py`
router.

## What was built

- `app/api/public_intake.py` — new `GET /questions` route plus a local
  `_org_name_for_link(db, link) -> str` helper: a column-scoped
  `select(Organisation.name)` rather than `select(Organisation)`, since
  `dd_public`'s grant on `organisation` is column-restricted. Duplicated
  from P3-10's `app/api/public_uploads.py` (not yet merged onto this
  branch) rather than imported — same reasoning P3-10 itself used for
  duplicating `app/api/uploads.py`'s validation helper.
- `app/schemas/public_intake.py` — `IntakeQuestionResponse` (one question:
  `question_key`, `prompt`, `help_text`, `input_type`, `required`,
  `display_order`) and `IntakeQuestionsResponse` (`org_name`,
  `questions: list[...]`), matching the exact `questions_snapshot` shape
  `app/api/deals.py` builds at link-generation time.
- Ordering: the snapshot's stored `questions` list is already
  `display_order`-sorted at write time (`DealIntakeQuestionRepo.list_active()`
  orders by it), but the route still `sorted()`s defensively — a one-line,
  zero-cost invariant check, not an unverified assumption.
- Null `questions_snapshot` (the model type allows `dict | None`, though no
  real link is ever created without one — `app/api/deals.py` always
  populates it) returns `questions: []`, not a 500.
- `tests/test_public_questions_api.py` (new, 6 tests): happy path +
  `display_order` sort verified; the response's top-level and per-question
  key set matches exactly (the ticket's literal acceptance criterion —
  checked as an exact key-set assertion, not spot-checked fields); null
  snapshot → empty list; cross-org non-leakage (a session for org A's link
  never sees org B's questions); invalid session token → the same 404
  every other public route returns; a validly-signed session token naming
  a genuinely nonexistent link → same 404.

## Verification

- `uv run pytest` (full suite, real Postgres, **fresh** dev volume via
  `docker compose -f docker-compose.dev.yml down -v postgres && up -d
  postgres` + `alembic upgrade head`) — **1017 passed, 2 skipped, 1
  deselected** (the two known pre-existing environment artifacts already
  documented in the status doc's Flagged section).
- `uv run pyright` — 0 errors, 0 warnings, 0 informations.
- **A real, fixed-in-scope fragility found while verifying**: this ticket's
  own tests originally asserted `body["orgName"] == "Org A"` (hardcoded).
  That's wrong on any run where `tests/test_ai_audit_log_immutability.py`
  or `tests/test_human_audit_log_immutability.py` happen to run first in
  the same pytest session — both seed the same shared
  `clerk_org_id = "test-tenant-00000000"` organisation row under the name
  `"Test Org"` via their own local fixtures (not `conftest.py::org_a_id`,
  which inserts `"Org A"` via `ON CONFLICT (clerk_org_id) DO NOTHING` —
  whichever fixture path runs first wins the name, and none of the three
  tear the row down). Reproduced this reliably across two separate full
  fresh-volume runs (not just stale-volume pollution). **Fixed** by adding
  a small `_org_name(owner_conn, clerk_org_id)` helper that reads the
  organisation's *actual current* name and asserting against that instead
  of a hardcoded string — correct regardless of suite-wide run order,
  and doesn't touch the other files' fixtures. This is a fix to this
  ticket's own test robustness, not a fix to the underlying shared-fixture
  design (same category of pre-existing fragility as the already-
  documented `chunks`-table pollution bug — that broader pattern stays out
  of scope here). Verified: 1017/1017 on two independent fresh-volume full
  runs after the fix.

## Deviations

None from the ticket's literal text. The only implementation choice not
spelled out verbatim in the ticket (whether a null `questions_snapshot`
500s or returns an empty list) was resolved in favor of not crashing on a
type-level possibility the real data model never produces — consistent
with this project's general posture elsewhere (e.g. P3-10's `DataSource`
handling).
