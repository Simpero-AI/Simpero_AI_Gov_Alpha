# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                              # install / sync dependencies
docker compose up --build            # start PgBouncer + app (port 8000) + SAQ worker
uv run uvicorn app.main:app --reload # run app locally without Docker
uv run alembic upgrade head          # apply migrations
uv run alembic revision --autogenerate -m "description"  # generate migration

uv run pytest                        # run all tests
uv run pytest tests/path/test_foo.py::test_name  # single test
uv run pyright                       # type checking

uv run saq app.jobs.worker.settings              # run the SAQ job worker
uv run saq app.jobs.worker.settings --web        # run the worker with the SAQ web dashboard (port 8080)
```

Tests require a real PostgreSQL instance — SQLite will not work because tests exercise RLS policies and `current_setting()`, which are PostgreSQL-specific.

## Architecture

### Two Postgres roles, strictly separated

| Role      | Privileges               | Used by                            |
| --------- | ------------------------ | ---------------------------------- |
| `doadmin` | DDL only                 | Alembic via `ALEMBIC_DATABASE_URL` |
| `dd_app`  | DML only, RLS-restricted | App at runtime via `DATABASE_URL`  |

`ALEMBIC_DATABASE_URL` connects **directly** to the DigitalOcean cluster, bypassing PgBouncer — DDL is not safe in transaction-pooling mode. `DATABASE_URL` routes through PgBouncer. Never swap them.

### Tenant isolation via `SET LOCAL` + RLS

Every authenticated request flows through the `get_db` dependency (`app/dependencies.py`), which:

1. Verifies the Clerk JWT and extracts `org_id` (via `CLERK_TENANT_ID_CLAIM`).
2. Opens a transaction.
3. Issues `SET LOCAL app.org_id = '<org_id>'` as the **first** SQL statement.
4. Yields the session to the route handler.

RLS policies on each table filter rows using `current_setting('app.org_id')`. **Do not add `WHERE org_id = ...` clauses in application queries** — that would be redundant and would mask a broken RLS configuration.

`SET LOCAL` (not `SET` or `SET SESSION`) is mandatory because PgBouncer in transaction-pooling mode reclaims the backend connection after each transaction. `SET SESSION` would leak the tenant's `org_id` to the next client that receives that connection.

### PgBouncer + NullPool

SQLAlchemy uses `NullPool` (`app/database.py`) because PgBouncer is the connection pool. Maintaining SQLAlchemy's own pool on top would hold PgBouncer slots open between requests, causing connection exhaustion. PgBouncer is configured in transaction-pooling mode via `docker/pgbouncer.ini`.

Do not open DB connections at application startup — that would hold a PgBouncer slot indefinitely.

### Auth: Clerk JWT

`app/core/security.py::decode_clerk_jwt` is fully implemented: it fetches Clerk's JWKS (cached, 1h TTL), verifies the JWT signature with `python-jose`, and extracts the tenant org id and role from either the v1 (`org_id`/`org_role`) or v2 (`o.id`/`o.rol`) claim shape via `_extract_org`. Audience is deliberately not validated — Clerk's default session tokens carry no `aud` claim.

### Admin portal (`/api/admin`) — strictly separate from the product portal

A two-tier admin surface (platform admins — Simpero-internal staff who manage
client organizations — and per-org client admins) mounted under `/api/admin`,
entirely independent of the product API (`/api/deals` etc.). **Admin and
product-portal code must never share logic, dependencies, or guards — this
separation is deliberate and must be preserved on every change, not just at
initial build time:**

- Admin auth dependency: `app/core/admin_dependencies.py::get_admin_db` /
  `require_org_admin` / `require_platform_admin` — never the product
  dependency `app/dependencies.py::get_db`, even though both follow the same
  `SET LOCAL app.org_id` discipline.
- Admin identity: the `clerk_admin_users` table (`app/models/clerk_admin_user.py`,
  `app/repo/AdminUserRepo.py`) — never `users`/`UserRepo`. A client admin is
  admin-only *by default* — they don't get a product `users` row just for
  being invited as an admin — but a product member explicitly **promoted**
  to admin (`PATCH /admin/members/{user_id}` and its cross-org sibling) keeps
  their existing `users` row alongside their new/reactivated
  `clerk_admin_users` row. Dual identity is a real, supported state for that
  one path, not a bug — don't assume the two tables are mutually exclusive.
- Admin routers live under `app/api/admin/` and must not import from, or be
  imported by, product routers (`app/api/deals.py` etc.).
- Admin Clerk adapter: `app/services/admin/clerk_admin.py` — kept separate
  from any product-facing Clerk calls (the one shared exception is
  `app/core/security.py::fetch_clerk_organization`, reused read-only by both
  sides since it predates the admin portal).
- Admin request/response schemas live under `app/schemas/admin/` and are
  never reused for product-facing responses, even when the shape looks
  similar.
- **One deliberate, narrow exception to the "never share logic" rule:**
  `app/core/dependencies.py::_ensure_user_provisioned` (product-side JIT
  provisioning, nothing to do with `/api/admin`) reactivates a soft-deleted
  `users` row (`UserRepo.reactivate()`) when a member removed via the admin
  portal is later re-invited and logs back in. This isn't admin logic
  reaching into product code — `users.status`/`deactivated_at` is one shared
  column set that both the admin portal (writer of removals) and product JIT
  provisioning (writer of first-logins and now reactivations) legitimately
  need to keep consistent. If you're tempted to add a second such exception,
  don't assume this one licenses it — get it confirmed explicitly the way
  this one was, rather than treating it as precedent for convenience.

Authorization is **table-authoritative**: every admin guard checks the
`clerk_admin_users` row (`status == "active"`, `admin_type`), never the JWT
`org_role` claim directly. The JWT is trusted at exactly two points: inside
`get_admin_db`'s JIT provisioning step, to grant a brand-new admin row,
downgrade an existing one (D3 sync), or reactivate a previously-inactive one
on a fresh admin-role login — everywhere else (every real guard, every
route handler), the table is the source of truth, never `org_role` directly.

A platform admin's `get_admin_db` session stays clamped to their **own** org
(`app.org_id`) for the whole request, even on the cross-org routes
(`platform_invitations.py`, `platform_members.py`) — those reach a *different*
org only via the Clerk Backend API, never via a direct DB query, with one
exception: local writes that must legitimately touch the target org's rows
(role-change and removal on `platform_members.py`) use
`admin_dependencies.py::_set_org_scope` to re-issue `SET LOCAL app.org_id`
mid-transaction, re-pointing RLS at the target org for just those statements,
then back at the caller's own org before any audit write. This works because
`SET LOCAL` persists for the whole transaction and can be re-issued as many
times as needed — it is **not** a new transaction, and trying to open one
(`session.begin()`) inside a route handler will fail: `get_admin_db` already
holds one open transaction for the entire yielded request. If you add a new
cross-org local write, reuse `_set_org_scope` rather than re-deriving this.

Keeping this boundary strict is deliberate housekeeping, not caution for its
own sake: it lets the admin surface's auth model, RLS scoping, and session
lifecycle evolve independently of the product portal's, without either side
ever risking a change that destabilizes the other. When adding to either
surface, don't reach across it for a "quick reuse" of a model, repo, guard,
or schema that happens to look similar — duplicate the few lines instead.

Full design history, decisions, and open gaps for this feature:
`docs/plans/admin-portal-backend.md` (Rev. 3) and
`docs/implementations/2026-07-24-admin-portal-backend.md`.

### Audit log immutability

`UPDATE` and `DELETE` on `audit_log` are revoked from `dd_app` at the database level. Do not add application-level guards — they can be bypassed and give false assurance. The `AuditLog` model in `app/models/audit_log.py` is commented out pending the table's full implementation.

### Job queue: SAQ + DigitalOcean Managed Valkey

Background jobs run on [SAQ](https://github.com/tobymao/saq), backed by a DigitalOcean Managed Valkey instance (`VALKEY_URL`).

- `app/jobs/queue.py::get_queue` returns a process-wide `Queue` singleton (`Queue.from_url` is lazy — no connection opens at import time, same no-connections-at-startup rule as Postgres).
- `app/jobs/tasks/` holds job functions; register new ones in `app/jobs/tasks/__init__.py::functions`.
- `app/jobs/worker.py` exposes the `settings: SettingsDict` the SAQ CLI runs against (`uv run saq app.jobs.worker.settings`).
- `VALKEY_URL` must use `rediss://` (TLS) — DO Valkey rejects unencrypted connections from outside its private network. `?ssl_cert_reqs=none` on the URL matches the `sslmode=require` posture already used for `DATABASE_URL`/`ALEMBIC_DATABASE_URL` (encrypts, doesn't pin the CA).
- `GET /health/queue` verifies connectivity the same way `/health/db` does for Postgres.
- Run `uv run saq app.jobs.worker.settings --web` for the built-in dashboard (port 8080) — do not expose this port publicly without auth in front of it.
- `docker-compose.yml`'s `worker` service runs the SAQ worker in its own container (same image as `app`, command overridden) — the `app` service only enqueues jobs, it never executes them.

### Document parsing: split out into Simpero_Gov_AI_Services (2026-07-17)

Document parsing (Docling-based PDF/XLSX/DOCX parsing, formerly
`services/parser` + `tests/parser` in this repo) now lives in its own
standalone repo, **Simpero_Gov_AI_Services**, as a separate FastAPI service
with its own Dockerfile, dependency lockfile, and CI (the actual split was
done with `git filter-repo`, preserving history — see that repo's own
commits). This app does not import Docling or openpyxl. `pypdf` is a narrow,
sanctioned exception: it's a direct dependency here solely for
`app/services/uploads/pdf.py`'s synchronous page count on
`/uploads/{id}/complete` (so the frontend can reject an over-length PDF at
upload time) — never for document parsing/extraction, which stays split out.
See the note next to `dependencies` in `pyproject.toml`.

**Integration today: none, wired up.** As of this repo's copy of
Simpero_Gov_AI_Services, that service exposes only a synchronous
`POST /parse` HTTP endpoint (bytes in, parsed index out) — it has no queue,
no Valkey/SAQ dependency, and no worker process. `app/jobs/parse_client.py`
in this app (added 2026-07-17) was built against an earlier assumption of an
async SAQ worker on a shared `"parse"` Valkey queue; that worker does not
exist in the actual Simpero_Gov_AI_Services codebase, so `parse_client.py`
currently enqueues jobs nothing will ever consume. It is not called from
anywhere in `app/api/` yet either. Treat it as scaffolding for a decision
that hasn't been made, not as a working integration — before using it,
confirm with the team whether the parser will grow an async worker to match
it, or whether this app should instead call `POST /parse` synchronously
(simpler, matches what actually exists today, but ties the request to
Docling's per-document parse latency).

- **Claims schema.** `contracts/claims.schema.json` is duplicated in both
  repos — Simpero_Gov_AI_Services owns it and validates against it in CI;
  this repo pins a copy and runs the same contract test
  (`contracts/test_claims_contract.py`) against its own copy. No shared
  package owns it yet — if it changes, update both by hand; nothing catches
  drift across the two repos automatically.

A mismatch in the queue name is silent: a job gets enqueued and simply never
picked up, with no error surfaced on either side. `tests/test_parse_client.py`
pins `PARSE_QUEUE_NAME == "parse"` as a guard, but it can't catch drift on
the other repo's side — check both when touching this contract.

### Request flow

```
HTTP request
  → CORS middleware
  → route handler (e.g. app/api/deals.py)
      → get_db dependency
          → decode_clerk_jwt (verify Clerk JWT, extract org_id)
          → AsyncSessionLocal() → session.begin()
          → SET LOCAL app.org_id = '<org_id>'
          → yield session
      → SQLAlchemy query (RLS filters automatically by app.org_id)
  → response
```

### Adding a new resource

1. Define the SQLAlchemy model in `app/models/` (inherits `Base` from `app/database.py`).
2. Import it in `app/models/__init__.py` so Alembic sees it.
3. Generate and apply a migration.
4. Define Pydantic schemas in `app/schemas/`.
5. Add a router in `app/api/` using `Depends(get_db)` — tenant context is automatic.
6. Register the router in `app/main.py`.

The `deals` route (`app/api/deals.py`) is the reference pattern for authenticated, tenant-scoped endpoints.
