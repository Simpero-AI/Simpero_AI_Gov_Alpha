# P3-07 — `POST /api/public/intake/{token}/session` implementation summary

**Status:** done, verified against real Postgres, full suite green (modulo
pre-existing unrelated failures, see below).
**Ticket:** P3-07 (`docs/plans/external-deal-intake-link-p3-status.md`,
`docs/plans/external-deal-intake-link-phase-3.md`).
**Session:** 2026-08-27.

---

## What this is

The first route in the external-intake flow: an unauthenticated recipient of
a shareable link submits the email address the link was addressed to, and on
a match gets back a short-lived (30-minute), self-issued session JWT that
every subsequent P3 route (`/questions`, `/answers`, `/uploads/*`, `/submit`)
will require. On a mismatch, the endpoint bumps `deal_intake_link.failed_attempts`
and returns the exact same 404 every other failure mode in this flow returns
— unknown/expired/revoked/already-submitted token, or wrong email are all
byte-identical responses, so the endpoint can never be used as an oracle.
Rate-limiting/lockout on top of `failed_attempts` is P3-12, explicitly out of
scope here.

## What was built

- `app/repo/IntakeLinkRepo.py::bump_failed_attempt` — one atomic `UPDATE`
  (`failed_attempts = failed_attempts + 1, last_attempt_at = now()`), no
  SELECT-then-mutate. Satisfies the existing `intake_link_status_update` RLS
  policy (already in the DB from P1-03) as-is — no migration needed for this
  part.
- `app/schemas/public_intake.py` — `IntakeEmailVerifyRequest` (`{email}`,
  `pydantic.EmailStr`) / `IntakeSessionResponse` (`{sessionToken}` on the
  wire, `CamelModel` convention).
- `app/api/public_intake.py` — the route. Uses `get_public_link_db` (not
  `get_public_session_db` — this route is what *issues* the session, so it
  authenticates via the raw link token, not a session JWT yet). On mismatch,
  returns a `JSONResponse(404, ...)` directly rather than raising
  `HTTPException` — raising would propagate into `get_public_link_db`'s
  generator at its `yield`, and `session.begin()`'s exception-exit path would
  roll back both the `failed_attempts` bump and the audit-log write before
  the response goes out. Returning a `Response` subclass exits the generator
  cleanly and commits, and is confirmed compatible with
  `response_model=IntakeSessionResponse` on the decorator (FastAPI's
  `get_request_handler` never consults `response_model` when the handler
  itself returns a `Response`).
- `app/main.py` — router registered under `API_PREFIX`, alphabetically
  between `mandates` and `uploads`.
- `tests/test_public_intake_session.py` — case-varied email match, wrong
  email (with `failed_attempts`/audit-row assertions via `owner_conn`, which
  bypasses RLS since `dd_public` has no SELECT on `human_audit_log`), 3x
  repeat mismatch with no lockout, byte-identical 404 across every failure
  mode (unknown/expired/revoked/submitted token, wrong email), and
  cross-decoder rejection in both directions (`decode_clerk_jwt` rejects an
  intake-session token; `decode_intake_session_jwt` rejects a Clerk-shaped
  one).
- `tests/conftest.py` — `org_a_deal_id` / `pending_link_with_token` moved
  here from `tests/test_public_dependencies.py` (that file's original home),
  since this ticket's test module needs them too — same "more than one
  module needs it" trigger already used for `org_a_id`.

### Response schema shape

```jsonc
// request
{ "email": "recipient@org-a.example" }

// 200
{ "sessionToken": "<jwt>" }

// every failure mode (byte-identical)
{ "detail": "Not found" }
```

## The `human_audit_log` / `dd_public` / `implicit_returning` finding

**This is the part P3-10 and P3-11 will hit too — read this before touching
`HumanAuditRepo.append()` from a `dd_public`-scoped session.**

`dd_public`'s grant on `human_audit_log` is deliberately **INSERT-only, no
SELECT** (`8f2a4c6e9b31_dd_public_grant_matrix.py`'s own docstring: *"every
external action gets a row, same append-only idiom as dd_app's grant... just
narrower (no SELECT for dd_public)"*). P1–P2 built that grant and the
dependency layer but never actually exercised a `dd_public`-session write to
this table end-to-end. P3-07 is the first ticket that does, and it failed on
first real run:

```
sqlalchemy.exc.ProgrammingError: permission denied for table human_audit_log
[SQL: INSERT INTO human_audit_log (...) VALUES (...) RETURNING human_audit_log.id, human_audit_log.created_at]
```

Root cause, isolated with a raw `psycopg2` connection as `dd_public`
(bypassing SQLAlchemy): the exact same `INSERT` **without** `RETURNING`
correctly surfaces the real RLS check instead (`new row violates row-level
security policy`) — so the grant/policy are fine. The problem is that
Postgres requires `SELECT` on any column named in `RETURNING`, and
SQLAlchemy's ORM unconditionally appends `RETURNING id, created_at` to any
insert of a row with server-generated columns, so it could read the
generated values back onto the Python object after flush. `dd_public`
doesn't have that `SELECT`, by design (an unauthenticated intake respondent
must never be able to read this table).

**Rejected fix:** granting `dd_public` `SELECT` on `human_audit_log` —
inverts a deliberate boundary (that table holds an org's *entire* activity
trail, and unlike `deal_intake_link` there's no natural secret to scope a
narrow keyhole `SELECT` policy against). Confirmed rejected by architect +
Vansh.

**Applied fix**, `app/models/human_audit_log.py`, two changes to the
`HumanAuditLog` model:

1. `__table_args__ = {"implicit_returning": False}` — a genuine SQLAlchemy
   `Table`-level option; stops the ORM from ever appending `RETURNING` to
   inserts on this table, for any role. No grant/RLS/migration changes
   needed anywhere.
2. **Not sufficient by itself** — disabling `RETURNING` also means the ORM
   has no way to learn the server-generated UUID primary key
   (`server_default=func.gen_random_uuid()`) after flush, and without it
   `session.flush()` fails differently: `FlushError: Instance ... has a NULL
   identity key` (SQLAlchemy can't register the flushed object as
   persistent without knowing its PK). Fixed by adding a **client-side**
   default alongside the existing server-side one:
   ```python
   id: Mapped[uuid.UUID] = mapped_column(
       UUID(as_uuid=True),
       primary_key=True,
       default=uuid.uuid4,
       server_default=func.gen_random_uuid(),
   )
   ```
   `default=uuid.uuid4` computes the id in Python before the INSERT is even
   sent, so the ORM already knows the PK — no RETURNING needed for it.
   `server_default` stays as-is, as a DB-level fallback for any insert that
   bypasses the ORM (raw SQL, another service). No migration needed — same
   column type, same value shape (random UUID), same DB-level default as
   before.

Net effect: `HumanAuditRepo.append()` now works identically from both
`dd_app` and `dd_public` sessions. No caller anywhere in the app reads
`created_at` off a just-appended `HumanAuditLog` object (confirmed by grep
before applying), so losing that one attribute's post-flush population is a
true no-op; `id` is still populated (via the new client-side default), so
anything that *does* read `.id` right after `append()` keeps working
unchanged.

**If you're implementing P3-10 or P3-11**: this fix is already in place on
`human_audit_log`. You don't need to re-derive it or touch the model again
— just call `HumanAuditRepo(session).append(...)` from your `dd_public`
session as normal.

## Verification

- `uv run pyright` — 0 errors, 0 warnings, 0 informations.
- `uv run pytest` (full suite, real Postgres via
  `docker compose -f docker-compose.dev.yml`) — **718 passed**, 5 failed, 9
  errors. All remaining failures are pre-existing and unrelated to this
  ticket:
  - `tests/test_chunks_rls.py` (3 failed + 5 errors) and
    `tests/test_e2e_pipeline.py` (4 errors) — this local dev Postgres
    instance is missing the `chunks` table / pgvector setup
    (`UndefinedTableError: relation "chunks" does not exist`), unrelated to
    intake.
  - `tests/test_l2_retrieval_eval.py::test_retrieval_meets_the_no_regression_floor`
    — same underlying cause.
  - `tests/test_public_intake_pool.py::test_missing_public_database_url_raises_validation_error`
    — an artifact of this worktree needing a real `.env` to run against
    Postgres at all (none existed before this session); with a `.env`
    present, `Settings()` falls back to it even after
    `monkeypatch.delenv`, so this one negative-path test no longer holds
    locally. Not a code regression — in CI (env vars injected directly, no
    `.env` file), this test's assumption holds.
- `tests/test_public_intake_session.py` — all 6 pass.
- `tests/test_human_audit_log_immutability.py` — all 3 pass (one transient
  failure during this session was leftover audit rows from repeated manual
  `pytest` invocations against this persistent local dev DB piling up under
  the shared test org — expected, since audit rows are correctly
  undeletable by `dd_app`/`dd_public` by design; cleaned up via `doadmin`,
  not a code issue).

## Deviations from the plan (both mechanical, not design calls)

- `app/schemas/intake_link.py`, which the plan said to mirror for
  `app/schemas/public_intake.py`'s style, doesn't exist in this worktree.
  Used `app/schemas/intake_question.py`'s `CamelModel` convention instead
  (same pattern, no functional difference).
- Fixture sharing: moved `org_a_deal_id` / `pending_link_with_token` from
  `tests/test_public_dependencies.py` into `tests/conftest.py` rather than
  duplicating them, following this repo's own established precedent
  (`conftest.py::org_a_id`'s docstring).
