# External deal intake link — Phase P1 implementation summary

**Status:** all ten tickets (P1-00 through P1-09) done and verified against
real Postgres. Held uncommitted except P1-00 (`44126da`, local/unpushed),
per Vansh's standing commit hold.
**Tickets:** P1-00, P1-01, P1-02, P1-03, P1-04, P1-05, P1-06, P1-07, P1-08,
P1-09.
**Source spec:** `docs/plans/external-deal-intake-link-implementation-brief.md`.
**Full ticket-by-ticket detail:** `docs/plans/external-deal-intake-link-status.md`.
**Session:** 2026-08-25/26.

---

## What this is

P1 is the database-level foundation for letting an external, unauthenticated
party (a deal counterparty) upload documents into this system via a shared
link, without ever holding a Clerk session. The brief's framing, worth
repeating: that party can touch exactly five tables, exactly the rows
belonging to one deal, and nothing else — provably, at the database level,
not by code review or app-layer checks. P1 builds and proves that boundary
in isolation, with no routes and no frontend on top of it yet.

## What was built

- **`dd_public` Postgres role** (third login role, alongside `doadmin` and
  `dd_app`) and its grant matrix: SELECT + narrow UPDATE(`status`,
  `submitted_at`, `failed_attempts`, `last_attempt_at`) on
  `deal_intake_link`; INSERT-only (no SELECT) on `deal_intake_response`;
  SELECT + INSERT on `data_source`; SELECT on a few `organisation` columns;
  INSERT on `human_audit_log`. Nothing else — enforced by an exact-grant
  drift test, not a sampled one.
- **`deal_intake_link` / `deal_intake_response` tables** (sections 2.2/2.3 of
  the brief), both RLS-enabled and FORCE'd, `deal_intake_link` with a
  one-way status trigger and a partial unique index keeping at most one
  `pending` link per deal.
- **RLS keyhole policies** (`intake_token_lookup`, `intake_session_lookup`,
  plus twins on `data_source`/`organisation`/`deal_intake_response`) that let
  `dd_public` see exactly one link/deal's rows, keyed off a GUC set inside
  the transaction — never off an app-supplied `WHERE`.
- **Two public dependency functions**, `get_public_link_db` (raw
  shareable-token path) and `get_public_session_db` (post-verification
  session-JWT path), both in `app/core/public_dependencies.py`, plus the
  self-issued intake-session JWT codec (`app/core/intake_security.py`).
- **A separate connection pool**, `PublicAsyncSessionLocal` bound to
  `dd_public`, `NullPool`, imported by nothing but
  `app/core/public_dependencies.py`, with its own PgBouncer stanza.
- **Negative test suites** proving the boundary from both directions: role
  privilege (every out-of-scope table denied, table by table) and RLS
  scoping (cross-tenant, no-GUC, expired/revoked/submitted, and
  `BYPASSRLS`/superuser absence) — culminating in P1-05's end-to-end suite
  run through the actual dependency functions, not raw policy predicates.

## Core design: the RLS handshake

The property the whole design rests on (brief section 4): tenant scope for
an unauthenticated caller is **derived from the database**, never asserted
by the request. A caller presents a bearer credential — either the raw
shareable token (hashed and looked up) or a session JWT issued after the
token was already validated once — and the dependency function resolves
that credential to exactly one `deal_intake_link` row, reads `org_id` and
`deal_id` off *that row*, and only then issues `SET LOCAL app.org_id` /
`app.intake_deal_id` for the rest of the transaction. RLS keyhole policies
on the downstream tables gate on those GUCs, so even a caller that could
somehow forge a GUC value still can't see past what the row lookup itself
authorized. Full design: brief section 4 (4.2 policies, 4.3 dependency
functions, 4.5 connection pool).

## Two design corrections made during implementation

1. **`organisation` grant needed `id` added.** The brief's original grant,
   `SELECT (name, clerk_org_id) ON organisation TO dd_public`, was reasoned
   about only as "two display columns," but Postgres evaluates RLS
   `USING`/`WITH CHECK` subqueries under the *querying* role's own column
   privileges — and three keyhole policies join `organisation` on `id` to
   resolve `clerk_org_id -> org_id`. Without `id` granted, every one of
   those policies failed outright (`permission denied`), not just
   RLS-filtered to zero rows. Fixed by widening the grant to `SELECT (id,
   name, clerk_org_id)`. Full diagnosis: status file, P1-00 row and Flagged
   section.

2. **`intake_session_lookup` deliberately widened to admit `status =
   'submitted'`** (an intentional asymmetry vs. `intake_token_lookup`, which
   stays `pending`-only). Postgres 16 requires an UPDATE's *resulting* row
   to satisfy an applicable SELECT policy for the role, not just the
   UPDATE's own `WITH CHECK` — so with both keyhole SELECT policies
   restricted to `status = 'pending'`, no UPDATE could ever flip a link to
   `submitted`, including via the link-id path the design explicitly means
   to allow. Resolved by widening only `intake_session_lookup`: the raw
   shareable token still dies the instant `status` leaves `pending`, but the
   session-JWT path (reachable only after a verified JWT names that exact
   `link_id`, never by guessing a UUID) can still see the link once
   submitted. Proven by two dedicated asymmetry tests rather than left
   implicit. Full diagnosis and repro: status file, P1-03 row and Flagged
   section.

## Current state

All ten P1 tickets are done and verified against real Postgres
(`docker-compose.dev.yml`, port 5434). Everything is held uncommitted except
P1-00 (`44126da`, local/unpushed), per Vansh's commit hold — nothing further
should be pushed or squashed without his go-ahead. The one open item
requiring manual action is P1-07's DigitalOcean-console confirmation of the
cluster's `max_connections` capacity for staging/production, deferred for
lack of console access this session. P2/P3 (actual routes, frontend,
malware scanning per OPEN-2) are explicitly out of scope for P1 and
untouched.

See `docs/plans/external-deal-intake-link-status.md` for full
ticket-by-ticket detail (commits, test counts, exact SQL) and
`docs/plans/external-deal-intake-link-implementation-brief.md` for the
original spec.
