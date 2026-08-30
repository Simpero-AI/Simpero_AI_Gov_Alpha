# P3-10 — Public: presigned-url + complete uploads

**Status:** done, verified against real Postgres, full suite green (973/973,
modulo two known pre-existing environment artifacts, see below).
**Ticket:** P3-10 (`docs/plans/external-deal-intake-link-p3-status.md`,
`docs/plans/external-deal-intake-link-phase-3.md`, lines 188-197).
**Session:** 2026-08-28. Picked up by Vansh's session directly (normally
Suraj's ticket — Suraj's wave-0 tickets hadn't started, and P3-10 unblocks
P3-15/P3-11/P3-13). Flagged `review=True` in the original spec's OWNERS
dict — extra scrutiny applied to the RLS/session-scoping and concurrency
logic specifically, see Verification below.

---

## What this is

Two new public, session-authenticated routes —
`POST /api/public/intake/uploads/presigned-url` and
`POST /api/public/intake/uploads/{upload_id}/complete` — letting an external
intake-link recipient upload documents without ever authenticating via
Clerk. Structurally parallel to the existing authenticated
`app/api/uploads.py`, reusing its Spaces service functions
(`build_object_key`, `presign_put`, `head_object`) and the same 10 MB /
extension-allowlist validation, but a deliberately separate router — not a
second auth branch on `/api/uploads/*` — reached via
`app/core/public_dependencies.py::get_public_session_db` (P1-06/P3-07),
the first route in this codebase to actually use that dependency.

`deal_id` and `org_id` come entirely off the verified session's link row,
never the request body — the public client cannot name a different deal or
organisation.

## The design decision this ticket needed before implementation

`DataSource` had no column tying a row to a specific intake link — only
`org_id`/`deal_id`. The spec's "20-file-per-link ceiling" therefore had two
possible readings: count `data_source` rows by `deal_id` (no schema change,
but conflates this link's uploads with anything the org side uploads
concurrently through the authenticated path — a real behavioral bug, not
just an approximation), or add a real `intake_link_id` column. Chose the
latter — see "What was built" below.

## What was built

- **Migration** `alembic/versions/9a48cce5ecac_data_source_intake_link_id.py`
  (`down_revision = 76a165315331`): adds `data_source.intake_link_id`
  (nullable UUID, FK to `deal_intake_link`, indexed). Also **tightens**
  `intake_deal_documents_insert`'s `WITH CHECK` (the `dd_public` INSERT
  policy on `data_source`, originally from `b4f8e1c3a962`) to additionally
  require `intake_link_id = current_setting('app.intake_link_id', true)::uuid`
  — closes a gap where a public session could otherwise write an arbitrary
  `intake_link_id` value for any other still-pending link on the same deal,
  not just its own. `alembic heads` stays single throughout.
- `app/models/data_source.py` — `intake_link_id` column, NULL for org-side
  authenticated uploads (`app/api/uploads.py`, untouched by this ticket).
- `app/repo/DataSourceRepo.py` — two new methods, `create()` untouched:
  - `count_for_intake_link(link_id) -> int` — plain unlocked count.
  - `try_create_for_intake_link(link_id, data, ceiling) -> DataSource | None`
    — the real ceiling enforcement. Takes a **transaction-scoped Postgres
    advisory lock** (`pg_advisory_xact_lock(hashtextextended(link_id, 0))`)
    before counting, so two concurrent `/complete` calls for the same link
    can't both observe `count == 19` and both insert, landing at 21. Chosen
    over `SELECT ... FOR UPDATE` because there's no existing row to lock at
    a link's *first* upload — advisory locks are the standard idiom for
    serializing around a value with no row yet. Auto-releases at
    COMMIT/ROLLBACK, safe under PgBouncer transaction pooling (same
    discipline as `SET LOCAL`).
- `app/api/public_uploads.py` (new router, `prefix="/public/intake/uploads"`,
  registered in `app/main.py` alongside `public_intake.router`):
  - `/presigned-url`: existing size/type validation + dedupe check (reused
    from the authenticated pattern, deal-scoped — correct for dedup,
    unrelated to the per-link ceiling), then `count_for_intake_link` — 409
    **before** `build_object_key`/`presign_put` are ever called if at the
    20-file ceiling. This check is a UX courtesy only, not the real
    boundary (see below).
  - `/complete`: re-derives `storage_key` deterministically, confirms via
    `head_object`, then `try_create_for_intake_link` (the actual,
    advisory-locked boundary) — 409 if it returns `None`. On success,
    enqueues `ingest_data_source` on the same SAQ queue with the same
    `timeout=120, retries=2` as the authenticated path (not explicit in the
    ticket text, but implied by "byte-for-byte identical `data_source`
    row" — the row needs the same downstream processing), then writes one
    `HumanAuditRepo` row: `event_type=intake_document_uploaded`,
    `actor_email` = the verified session email.
  - A small `_decode_claims` dependency re-decodes the same session token
    `get_public_session_db` already verified, purely to reach
    `claims.email` for the audit row — chosen over changing
    `get_public_session_db`'s yielded shape, since that's a pinned contract
    shared by every other P3 public route (and its own tests). The re-decode
    is a cheap local HS256 verify, no network call; `AuthenticationError` is
    caught and mapped to the same uniform 404 every other public-route
    failure mode returns, matching `get_public_session_db`'s own convention.
- `app/schemas/public_uploads.py` (new) — `PublicPresignRequest`,
  `PublicCompleteRequest`. Neither carries `deal_id`.
- `tests/test_public_uploads_api.py` (new, 9 tests) — 21st-presign rejection
  (before any Spaces call), happy-path presign, `/complete` row-shape parity
  with the authenticated path (`intake_link_id` set, everything else
  matching), exactly-one-audit-row-per-complete with the correct
  `actor_email`, missing-PUT 4xx with no row/audit, at-ceiling `/complete`
  409 with no enqueue/no audit, cross-org isolation (a session for one
  org/link cannot see another org's documents), and — the one that mattered
  most given the design decision above — **same-org, different-link
  ceiling isolation**: one link's presign-time ceiling check is unaffected
  by another link's (or the org's authenticated-path) `data_source` rows on
  the same deal.
- `tests/test_dd_public_grant_drift.py` — updated the pinned
  `EXPECTED_COLUMN_PRIVILEGES` set to include `intake_link_id`'s
  SELECT/INSERT (inherited automatically from the existing whole-table
  grant on `data_source`, P1-00 — no new `GRANT` statement needed).

## Verification

- `uv run pytest` (full suite, real Postgres via
  `docker compose -f docker-compose.dev.yml up -d postgres`, fresh volume,
  `alembic upgrade head`) — **973 passed, 2 skipped**. Deselected/ignored
  the two known pre-existing, unrelated local-environment artifacts already
  documented in `docs/plans/external-deal-intake-link-p3-status.md`'s
  Flagged section: `tests/test_public_intake_pool.py::test_missing_public_database_url_raises_validation_error`
  (checked-in `.env` fallback, local-only, passes in CI) and the
  `tests/test_memory_scope_rls.py`/`tests/test_retrieval_rls.py` `DROP
  TABLE chunks CASCADE`-without-recreate pollution. One additional
  transient failure (`tests/test_human_audit_log_immutability.py::test_dd_app_can_insert_and_select_human_audit_log`)
  was seen once on a non-fresh volume and did not reproduce on a fresh one
  or in a subsequent full run — matches the exact same flaky pattern
  already recorded in P3-12's own implementation doc ("leftover audit rows
  from repeated manual test runs against this persistent local dev DB"),
  not a regression from this ticket.
- `uv run pyright` — 0 errors, 0 warnings, 0 informations.
- Extra scrutiny applied given this ticket's `review=True` flag: manually
  traced the new `intake_deal_documents_insert` policy body against the
  original (`b4f8e1c3a962`) to confirm the `downgrade()` restores it
  exactly; confirmed `app.intake_link_id` is in fact set by
  `get_public_session_db` (it is, Phase 1 of that dependency) so the
  tightened `WITH CHECK` clause has a real GUC to compare against; read the
  full router diff end-to-end for anything accepting `deal_id`/`org_id`
  from client input (nothing does).

## Deviations / judgment calls worth flagging

- **SAQ enqueue on `/complete`**: not explicitly named in the P3-10 ticket
  text, added because the ticket's stated acceptance criterion ("the
  resulting `data_source` row is byte-for-byte identical in shape to one
  created by the authenticated org-side path") implies the same downstream
  ingest processing should run too. If that's not actually wanted (e.g. if
  public uploads should stay `pending` and get ingested some other way),
  this is the one thing in this ticket to double-check against product
  intent.
- **Data-model decision** (add `intake_link_id` vs. count by `deal_id`) —
  covered above, made via a dedicated architect pass given it wasn't fully
  pinned by the spec text. Flagging again here since it's the one place
  this implementation extends the schema beyond what the ticket's own text
  literally specifies.
