# Admin Portal — Backend Implementation Architecture Plan (Rev. 3)

**Repo:** `/Users/vanshkhanna/Documents/Simpero/Simpero_AI_Gov_Alpha`
**Status:** For implementer subagent. Standalone, phased, precise. All decisions verified against the code; discrepancies called out in-line.
**Supersedes:** Rev. 2 of this file. If the two disagree, this document wins.

---

## What changed in this revision (read first)

Rev. 3 folds three now-final decisions into the body and **deletes the "pending integration" Addendum** — its content is fully integrated below.

- **D1 — `redirect_url` differs by invited role.** `create_organization_invitation` no longer hardcodes `<app_base_url>/sign-up`; it takes `redirect_url` as a parameter. Client-admin seed invites (`org:admin`) land on `<app_base_url>/admin/sign-up` (the dedicated admin sign-up route that completes the Clerk ticket into the `/admin` portal); product-user invites (`org:member`) land on `<app_base_url>/sign-up` (product dashboard). Both paths derive from `app_base_url`.
- **D2 — new platform-admin endpoint: invite a product user into a specified client org** (`POST /api/admin/organizations/{clerk_org_id}/invitations`, guard `require_platform_admin`). This is the **one deliberate, guarded exception** to "target org comes from the token": the target org is the `{clerk_org_id}` path param, because the platform admin's token is scoped to the Simpero org. It is safe because it is a Clerk Backend-API call (create invitation), **not a DB write** — it never hits the org-isolation RLS wall — and it is locked behind `require_platform_admin`. Lives in a new `platform_invitations.py`.
- **D3 — R6 downgrade-only sync is now BUILT (was an open risk).** `_ensure_admin_provisioned` gains a downgrade-only reconciliation: an existing **active** admin row whose current JWT `org_role != "admin"` is flipped to `status = 'inactive'`. It can only revoke, never upgrade/re-activate. This makes a Clerk demotion/removal revoke portal access on the next request. `AdminUserRepo` gains a `deactivate(clerk_user_id)` method.

Rev. 2's structural changes still stand and are reproduced: admin identity lives in a dedicated `clerk_admin_users` table that is the authorization source of truth (not the JWT `org_role`); a single Alembic migration; a separate `get_admin_db` dependency; audit-actor resolution off the admin row; and the JWT `org_role` trusted in exactly one place (`_ensure_admin_provisioned`).

Consequences carried over from Rev. 2:

1. **The original "no new tables, no migration, no `dd_owner` DDL" claim is VOIDED.** This feature ships **exactly one** Alembic migration: `clerk_admin_users` (table + enum + RLS policy). No existing RLS policy or product table changes.
2. Admin routes use a **separate DB dependency `get_admin_db`** that clamps RLS and JIT-provisions the admin row **without** creating a product `users` row. Client admins are **admin-only**.
3. **Audit-actor resolution** uses the `clerk_admin_users` row, not `UserRepo.get_by_clerk_id`.
4. The JWT `org_role` is trusted in **exactly one place**: the JIT bootstrap + downgrade sync inside `_ensure_admin_provisioned`. Guards authorize off the table.

---

## Overview

A two-tier admin portal mounted at `/api/admin`.

- **Platform admins** — Simpero-internal, members of the dedicated Simpero Clerk org (`settings.simpero_platform_org_id`). They create client organizations in Clerk and seed exactly one account-manager admin per org via a Backend-API invitation carrying a `redirect_url` (the whole reason for the feature — Dashboard-created invitations can't set one). They can **also** invite `org:member` product users into any client org (D2).
- **Client admins** = account managers, `org:admin` in a client org. They invite/manage `org:member` users **within their own org only**.

Clerk remains the source of truth for the org registry; the local `organisation` row stays lazily JIT-provisioned. Admin **identity** is persisted in the new `clerk_admin_users` table. All admin code lives in dedicated packages, sharing only JWT primitives, ORM models, and the audit repo with product code.

---

## Verified findings (file:line)

**Routing / mount**
- `app/main.py` — single `API_PREFIX = "/api"`; every router included at that prefix. No `/v1`. Admin mounts with one `include_router(admin.router, prefix=API_PREFIX)` line (router carries `prefix="/admin"`). Exception handlers map `AuthenticationError→401`, `AuthorizationError→403`, `TenantContextError→401` — guards raise these.

**Auth / claims**
- `app/core/security.py:51-100` — `decode_clerk_jwt` returns `{"tenant_id", "user_id", "org_role", "raw_claims"}`. `tenant_id` **is the Clerk org id**; `org_role` is prefix-stripped (`"admin"`/`"member"`, `security.py:42,47`). Both v2 (`claims["o"]["id"]/["rol"]`) and v1 (`org_id`/`org_role`) shapes handled (`security.py:32-48`).
- `app/core/security.py:103-111` — `fetch_clerk_organization`: raw `httpx.AsyncClient(timeout=5.0)`, base `https://api.clerk.com/v1`, `Authorization: Bearer {settings.clerk_secret_key}`, `raise_for_status()`. **The Clerk adapter idiom to copy.** Because it `raise_for_status()`es, it doubles as the existence check for the D2 `{clerk_org_id}` path param (404 → 404).
- `app/core/dependencies.py:17-38` — `get_claims` verifies the bearer token; 401 on bad header, 503 if JWKS unreachable.

**Tenant clamp + product JIT provisioning (the pattern `get_admin_db` mirrors)**
- `app/core/dependencies.py:92-129` — `get_db` opens one transaction, issues `SELECT set_config('app.org_id', :tid, true)` (`:tid = claims["tenant_id"]`) as the **first** statement, then `_ensure_user_provisioned`, then yields, commits on success. The GUC is **`app.org_id`**; the claim key is **`tenant_id`** — never introduce `app.tenant_id`. **Re-verified for D2:** the clamp binds `claims["tenant_id"]`, so a platform admin's admin session is clamped to the Simpero org — audit rows land in the Simpero trail.
- `app/core/dependencies.py:41-89` — `_ensure_user_provisioned`: on first login, looks up the `Users` row by `clerk_user_id` and **short-circuits `if user_id is not None: return`** (`:49-51`); if the `Organisation` row is absent it fetches from Clerk (`fetch_clerk_organization`, reading `public_metadata.type` into `OrgType`) and `pg_insert(...).on_conflict_do_nothing`, then upserts the `Users` row. `name`/`email` stay NULL — the token carries neither; the frontend backfills via `POST /auth/sync-profile`. That short-circuit is the analogue D3 must **restructure** (the admin path must run the downgrade sync even when a row exists). **This is also why admin email must be fetched from Clerk during admin provisioning: the session token has no email.**

**RLS pattern to copy for the new tenant table**
- `alembic/versions/aace95a1c412_rls_policies.py:27-31` — `organisation`: `CREATE POLICY org_isolation ... FOR ALL TO dd_app USING (clerk_org_id = current_setting('app.org_id', true))`. Enum created inline via `sa.Enum(...)`.
- `alembic/versions/c9aaf2c46b16_...:58-63` — `users` has the identical `FOR ALL` policy; comment (`:55-57`): *"FOR ALL derives WITH CHECK from USING, so INSERTs … are only allowed for the request's own org."* **This is the exact policy `clerk_admin_users` gets.** Note: `users`/`organisation` use plain `ENABLE ROW LEVEL SECURITY` — **no `FORCE`**, no explicit `GRANT`, no `REVOKE`.
- `alembic/versions/7175bc85ffb0_human_audit_log.py:68-89` — `human_audit_log` additionally uses `FORCE ROW LEVEL SECURITY` + `REVOKE UPDATE, DELETE ... FROM dd_app`. That `FORCE`/`REVOKE` combo is **specific to immutability** and does **not** apply to `clerk_admin_users`, which is a mutable tenant projection like `users`.
- `alembic/versions/bootstrap_dd_app_privliges.py:11-22` — `ALTER DEFAULT PRIVILEGES FOR ROLE doadmin ... GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dd_app`. Any table created by that role auto-grants `dd_app` full DML. So `clerk_admin_users` needs **no explicit GRANT** as long as the migration runs under the role the default-privileges block covers. Confirms runtime role is `dd_app`, DDL is a separate role.

**Repos / models / schemas**
- `app/repo/BaseRepo.py:10-19` — abstract `create`/`get_by_id`. `app/repo/UserRepo.py:22-34` — `get_by_clerk_id` and `upsert` (`pg_insert ... on_conflict_do_nothing(index_elements=["clerk_user_id"])`). **`AdminUserRepo` mirrors this** and adds `deactivate` (D3).
- `app/models/organisation.py:15-61` — `OrgType` enum (values `"PE Firm"`/`"Family Office"`); `Organisation` (id serial, `clerk_org_id` unique, `type` nullable, `created_at` default `utc_now`); `Users` (`org_id` FK **NOT NULL**, `email`/`name` nullable, `clerk_user_id` unique, `clerk_org_id` indexed).
- `app/schemas/common.py:5-13` — `CamelModel` (alias_generator `to_camel`, `populate_by_name`, `from_attributes`). All admin response schemas extend it.
- `app/models/human_audit_log.py:33-40` — `org_id` Integer FK NOT NULL; `actor_id` String(64) nullable; `actor_email` String(100) nullable. INSERT-only, DB-enforced.
- `app/repo/HumanAuditRepo.py:18-23` — `append(data)` is the sole write path (INSERT-only; DB `REVOKE UPDATE, DELETE ON human_audit_log FROM dd_app` enforces immutability regardless of app code); never add update/delete.

**Audit-actor pattern (what shifts)**
- `app/api/history.py:18-22` — `_actor(db, claims)` resolves `(org_id, actor_id, actor_email)` via `UserRepo(db).get_by_clerk_id(claims["user_id"])`. `app/api/auth.py:59-66` — same. **Admins have no `users` row, so admin routers resolve the actor from the `clerk_admin_users` row instead (below).**

**Config**
- `app/core/config.py:6-20` — pydantic-settings, `env_file=".env"`, `extra="ignore"`, `@lru_cache`. `clerk_secret_key` present. `simpero_platform_org_id` / `app_base_url` do **not** exist yet.

**Tests**
- `tests/conftest.py:19-32` — `owner_conn` (doadmin psycopg2, bypasses RLS, for seeding a second tenant). `:35-61` — `db_session` replicates `get_db`'s `SET LOCAL app.org_id`. DB-backed tests require a real Postgres.
- `tests/test_phase1_endpoints.py:25-49` — endpoint idiom: override `get_claims` via `app.dependency_overrides`; `ApiTestClient` auto-prefixes `/api`; orgs seeded through `owner_conn`; `_claims` shows the dict shape. Audit side-effects asserted by querying `human_audit_log` through `owner_conn`.

---

## API contract

Base path `/api/admin`. Auth: existing `get_claims` (Clerk bearer). All responses extend `CamelModel` (camelCase on the wire). Error bodies are `{"detail": "<message>"}`. All admin routes use `get_admin_db` (not `get_db`).

### Capability / context
- **`GET /api/admin/context`** → `200`
  `{ isPlatformAdmin: bool, isOrgAdmin: bool, org: { clerkOrgId: str, name: str, type: "PE Firm" | "Family Office" | null } }`
  Derives `isPlatformAdmin` server-side by comparing `claims["tenant_id"]` to `settings.simpero_platform_org_id` — the platform org id is **never** returned to the client. `org` fields come from the caller's own RLS-scoped `organisation` row.

### Org-admin endpoints — guard `require_org_admin`; target org = `claims["tenant_id"]` (never from body)
- **`POST /api/admin/invitations`** body `{ emailAddress: str (EmailStr), role?: "member" }` → `201`
  Creates a Clerk org invitation into the **caller's** org, role forced to `org:member`, **`redirect_url = <app_base_url>/sign-up`** (product sign-up → product dashboard; D1). `role` is `Literal["member"]` defaulting to `"member"`; any other value → **422** (schema) and an explicit endpoint guard also rejects with **403**. Returns `{ id: str, emailAddress: str, status: str, createdAt: datetime }`.
- **`GET /api/admin/invitations`** → `200` `list[{ id, emailAddress, role, status, createdAt }]` — pending invitations for the caller's org (Clerk, `status=pending`).
- **`DELETE /api/admin/invitations/{invitation_id}`** → `200` `{ success: true }` — revoke a pending Clerk invitation for the caller's org.
- **`GET /api/admin/members`** → `200` `list[{ id: int, clerkUserId: str, name: str | null, email: str | null, role: str }]` — from the RLS-scoped `users` table (caller's org only).
- **`DELETE /api/admin/members/{user_id}`** (`user_id` = local `users.id`, int) → `200` `{ success: true }` — see Member-removal semantics below.

### Platform-admin endpoints — guard `require_platform_admin`
- **`POST /api/admin/organizations`** body `{ name: str, type?: "PE Firm" | "Family Office", accountManagerEmail: str (EmailStr) }` → `201`
  Creates the org in Clerk (`public_metadata.type` from `OrgType`), then seeds **one** invitation with role `org:admin` and **`redirect_url = <app_base_url>/admin/sign-up`** (dedicated admin sign-up route → `/admin` portal; D1). **No local DB insert.** Returns `{ clerkOrgId: str, name: str, type: str | null, invitation: { id, emailAddress, status, createdAt } }`.
- **`GET /api/admin/organizations`** → `200` `list[{ clerkOrgId, name, type, createdAt }]` — client orgs from Clerk's Backend API, **excluding** the Simpero platform org.
- **`POST /api/admin/organizations/{clerk_org_id}/invitations`** *(NEW — D2)* body `{ emailAddress: str (EmailStr), role?: "member" }` → `201`
  Invites an `org:member` **product user into the client org named by the `{clerk_org_id}` path param** — a deliberate, guarded cross-tenant path. Role forced to `org:member`; **`redirect_url = <app_base_url>/sign-up`** (product user; D1). Returns `{ id, emailAddress, status, createdAt }`.
  - **Target org is the path param, NOT the token.** The platform admin's token is scoped to the Simpero org, so it cannot supply the client org via the token. This is the single documented exception to "target org comes from the token."
  - **Validation:** reject `{clerk_org_id} == settings.simpero_platform_org_id` → **403** `"Cannot invite into the platform org"`; validate the org exists via `fetch_clerk_organization({clerk_org_id})` → **404** on Clerk 404. `role` other than `"member"` → **422**/**403** (same guard pattern as `POST /admin/invitations`).
  - **Why it is safe:** it is a Clerk Backend-API `create invitation` call, **not a DB write** — it never touches org B's rows and therefore never hits the org-isolation RLS wall — and it is locked behind `require_platform_admin`. See the RLS/session note under Provisioning and the audit note below.

---

## Data model: `clerk_admin_users`

New ORM model `app/models/clerk_admin_user.py` (own module for isolation; imports `Base` from `app.core.database`). It is a **tenant table** and gets the exact `org_isolation` policy that `users`/`organisation` have.

| Column | Type | Constraints | Rationale |
|---|---|---|---|
| `id` | `Integer` | PK, index | Serial surrogate key, same idiom as `organisation`/`users`. |
| `clerk_user_id` | `String(64)` | **unique**, index, NOT NULL | The caller identity (`claims["user_id"]`). **The guard lookup key.** Unique ⇒ one admin row per human; also the `on_conflict` target for JIT provisioning and the key for `deactivate`. |
| `clerk_org_id` | `String(64)` | index, NOT NULL | **The RLS discriminator.** The `org_isolation` policy keys on `clerk_org_id = current_setting('app.org_id')`, identical to `users`. |
| `org_id` | `Integer` FK→`organisation.id` | NOT NULL, index | Integer FK so audit-actor resolution yields the Integer `org_id` the audit table expects. NOT NULL: `get_admin_db` provisions the `organisation` row *before* inserting the admin row. |
| `email` | `String(100)` | nullable, index, **non-unique** | Denormalized for audit/display. Filled from Clerk `GET /users/{id}` during provisioning (session token carries no email). Non-unique to avoid collisions if the same human later also becomes a product user (backlog). |
| `admin_type` | `Enum(platform, client)` name=`admintype` | NOT NULL | Authorization tier. `platform` iff `claims["tenant_id"] == settings.simpero_platform_org_id`, else `client`. Read by `require_platform_admin`. |
| `status` | `String(50)` | NOT NULL, default `'active'` | Lets an admin be **deactivated without deletion** (audit continuity). Guards require `status == 'active'`. The D3 downgrade sync flips this to `'inactive'`. |
| `created_at` | `DateTime` | default `utc_now` | Same as `organisation.created_at`. |

Model note: define `AdminType(enum.Enum)` with values `"platform"`/`"client"` in the same module; reuse `utc_now`. Register the new model where Alembic's `env.py` collects `Base.metadata`.

### Alembic migration outline (house style — matches `c9aaf2c46b16` + `aace95a1c412`)

One new file `alembic/versions/<rev>_clerk_admin_users.py`. Set `down_revision` to the **current head** (`alembic heads`). Runs as the DDL role (`doadmin`/`dd_owner`), never `dd_app`.

```python
def upgrade():
    op.create_table(
        "clerk_admin_users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("clerk_user_id", sa.String(64), nullable=False),
        sa.Column("clerk_org_id", sa.String(64), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(100), nullable=True),
        sa.Column("admin_type", sa.Enum("platform", "client", name="admintype"), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["organisation.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_clerk_admin_users_clerk_user_id"),
        "clerk_admin_users",
        ["clerk_user_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_clerk_admin_users_clerk_org_id"),
        "clerk_admin_users",
        ["clerk_org_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_clerk_admin_users_org_id"), "clerk_admin_users", ["org_id"], unique=False
    )
    op.create_index(
        op.f("ix_clerk_admin_users_email"), "clerk_admin_users", ["email"], unique=False
    )

    # RLS enabled in the SAME migration that creates the table (same idiom as users/funds).
    op.execute("ALTER TABLE clerk_admin_users ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY org_isolation ON clerk_admin_users
            FOR ALL TO dd_app
            USING (clerk_org_id = current_setting('app.org_id', true))
    """)


def downgrade():
    op.execute("DROP POLICY IF EXISTS org_isolation ON clerk_admin_users")
    op.execute("ALTER TABLE clerk_admin_users DISABLE ROW LEVEL SECURITY")
    op.drop_index(...)  # all four
    op.drop_table("clerk_admin_users")
    op.execute(
        "DROP TYPE IF EXISTS admintype"
    )  # sa.Enum auto-creates the pg type; drop on downgrade
```

Explicit decisions (RLS/tenant boundary — extra scrutiny):
- **No `FORCE ROW LEVEL SECURITY`, no `REVOKE`.** Mutable tenant projection like `users`, not the immutable audit trail.
- **No explicit `GRANT`.** The bootstrap `ALTER DEFAULT PRIVILEGES FOR ROLE doadmin` already grants `dd_app` SELECT/INSERT/UPDATE/DELETE. Confirm the migration runs under that role (see R5).
- `FOR ALL` derives `WITH CHECK` from `USING`, so a `dd_app` session clamped to org X can only INSERT/UPDATE/SELECT rows with `clerk_org_id = X`. Both the JIT insert and the D3 downgrade UPDATE are RLS-safe by construction (they only ever touch the caller's own-org row).

---

## Provisioning design

### `get_admin_db` (`app/core/admin_dependencies.py`)

A separate DB dependency for admin routes. Mirrors `get_db`'s RLS discipline exactly but provisions the **admin** row, not a product `users` row.

```python
async def get_admin_db(claims=Depends(get_claims)) -> AsyncSession:
    async with AsyncSessionLocal() as session, session.begin():
        # 1. FIRST statement — clamp RLS to the caller's own org.
        await session.execute(
            text("SELECT set_config('app.org_id', :tid, true)"),
            {"tid": claims["tenant_id"]},
        )
        # 2. Ensure the caller's OWN organisation row exists (RLS-safe: own org).
        await _ensure_org_provisioned(session, claims)
        # 3. JIT the clerk_admin_users row + run the downgrade-only sync. Does NOT touch `users`.
        await _ensure_admin_provisioned(session, claims)
        yield session
```

- **(a) SET LOCAL first.** Identical to `get_db:124-127`. Transaction-scoped, PgBouncer-safe.
- **(b) `_ensure_org_provisioned`** — the org branch of `_ensure_user_provisioned` (`dependencies.py:56-79`), extracted: if no `organisation` row for `claims["tenant_id"]`, fetch from Clerk and `pg_insert(...).on_conflict_do_nothing(index_elements=["clerk_org_id"])`, then re-select. RLS-safe (caller's own org). **Do not import `_ensure_user_provisioned`** (it also creates a `users` row) — extract/duplicate just the org branch.
- **(c) `_ensure_admin_provisioned`** — the JIT admin bootstrap **plus** the D3 downgrade-only sync. **The one and only place the JWT `org_role` is trusted.**

```python
async def _ensure_admin_provisioned(session, claims):
    repo = AdminUserRepo(session)
    existing = await repo.get_by_clerk_id(claims["user_id"])
    is_admin_now = claims.get("org_role") == "admin"

    if existing is not None:
        # R6 downgrade-only sync (D3): revoke on the next request after a Clerk
        # demotion/removal. ONLY ever revokes — never upgrades or re-activates.
        if existing.status == "active" and not is_admin_now:
            await repo.deactivate(claims["user_id"])  # status -> 'inactive'
        # An inactive row stays inactive even if org_role == "admin" again
        # (re-activation would need a future admin-management endpoint — see R6).
        return

    # No row yet — one-time JIT create. This is the only place org_role GRANTS access.
    if not is_admin_now:
        return  # not an admin per Clerk → no row → guard 403 (fail closed)
    org_pk = await session.scalar(
        select(Organisation.id).where(Organisation.clerk_org_id == claims["tenant_id"])
    )  # guaranteed present by step (b)
    email = await fetch_clerk_user_primary_email(claims["user_id"])  # Clerk GET /users/{id}
    admin_type = "platform" if claims["tenant_id"] == settings.simpero_platform_org_id else "client"
    await repo.upsert(
        {
            "clerk_user_id": claims["user_id"],
            "clerk_org_id": claims["tenant_id"],
            "org_id": org_pk,
            "email": email,
            "admin_type": admin_type,
            "status": "active",
        }
    )  # pg_insert(...).on_conflict_do_nothing(index_elements=["clerk_user_id"])
```

**D3 logic + ordering (RLS/auth boundary — extra scrutiny):**
- The Rev. 2 `if existing is not None: return` short-circuit is **restructured**: the sync now runs *inside* the existing-row branch **before** returning, so it fires on every request an already-provisioned admin makes. This is the deliberate departure from `_ensure_user_provisioned`'s straight short-circuit.
- The sync is **strictly monotonic toward less access**: it only transitions `active → inactive`, and only when Clerk currently says the caller is not an admin. It never sets `active` and never touches an already-`inactive` row. This keeps the JWT trusted for *de*-authorization only (the safe direction). Re-authorizing a re-promoted user is intentionally out of scope (needs a future admin-management endpoint).
- The `deactivate` UPDATE is RLS-safe: the session is clamped to the caller's own org, and `deactivate` targets the caller's own `clerk_user_id`, so the `org_isolation` `WITH CHECK` cannot be violated.
- The guards then see `status != "active"` → **403** on this same request.

Other invariants (carried from Rev. 2):
- **Does NOT call `_ensure_user_provisioned`** — an admin never gets a product `users` row. Client admins are **admin-only**.
- **Email backfill** from Clerk `GET /users/{id}` (session token carries no email; no admin-side `sync-profile`). If Clerk is unreachable, provision with `email = None` rather than failing (email is display/audit, refreshable later).
- **`admin_type` fixed at creation**; `require_platform_admin` cross-checks it against `simpero_platform_org_id` at read time.

**Which dependency each route uses:** product routers keep `get_db`; admin routers use `get_admin_db`. `GET /admin/members` reads the product `users` table (members ARE product users) — reading under the caller's RLS scope is correct; only *provisioning the admin as a product user* is avoided.

### RLS/session note for the D2 platform member-invite endpoint

`POST /api/admin/organizations/{clerk_org_id}/invitations` runs under `get_admin_db`, whose SET LOCAL clamps `app.org_id` to the **platform admin's own (Simpero) org** — the platform admin has no `clerk_admin_users` row in the client org and the session cannot see or write client-org rows. That is fine and intended:
- The **invitation is created via the Clerk Backend API** against `{clerk_org_id}` (a network call), so it never touches org B's Postgres rows and never engages the `org_isolation` policy on any tenant table.
- The **audit row is written in the Simpero trail** (see below), which is exactly where the clamped session is allowed to write.
So the DB session (Simpero-clamped) and the Clerk API target (org B) intentionally diverge; there is no RLS violation because nothing writes to org B's database.

### First platform admin — bootstrap (no migration seed)

A human is granted `org:admin` in the **Simpero Clerk org** via the dashboard, one time. On their first admin request, `get_admin_db` sees `org_role == "admin"` + `tenant_id == simpero_platform_org_id` and JIT-creates their row with `admin_type = "platform"`. No migration seed (would need an unknown `clerk_user_id` at migration time and duplicate the JIT path).

---

## Authorization design (`app/core/admin_dependencies.py`)

**The `clerk_admin_users` table is authoritative. Guards do not trust the JWT `org_role`.** They depend on `get_admin_db` (which has already provisioned the row for a legitimate admin and run the downgrade sync) and look up an **ACTIVE** row.

```python
async def require_org_admin(claims=Depends(get_claims), db=Depends(get_admin_db)) -> dict:
    row = await AdminUserRepo(db).get_by_clerk_id(claims["user_id"])
    if row is None or row.status != "active":
        raise AuthorizationError("Org admin privileges required")  # -> 403
    return claims


async def require_platform_admin(claims=Depends(get_claims), db=Depends(get_admin_db)) -> dict:
    platform_org = settings.simpero_platform_org_id
    if not platform_org:  # fail closed when unconfigured
        raise AuthorizationError("Platform admin surface not configured")  # -> 403
    row = await AdminUserRepo(db).get_by_clerk_id(claims["user_id"])
    if (
        row is None
        or row.status != "active"
        or row.admin_type != "platform"
        or claims["tenant_id"] != platform_org
    ):
        raise AuthorizationError("Platform admin privileges required")  # -> 403
    return claims
```

Key points (RLS/tenant/auth boundary — extra scrutiny):
- **RLS makes the lookup implicitly org-scoped.** `get_admin_db` clamps `app.org_id` to the caller's own org, so `get_by_clerk_id` can only return a row for the caller's org. No way to return another org's admin row.
- **`admin_type == "platform"` cross-checked against `simpero_platform_org_id`.** Both must agree.
- **Fail-closed** when `simpero_platform_org_id` unset — platform surface denies everyone; setting can default to `""`.
- A platform admin also satisfies `require_org_admin` but only ever acts on the Simpero org (their own clamped tenant) — harmless. Guards stay independent.
- **Downgrade takes effect via the guard:** after `_ensure_admin_provisioned` flips a demoted caller's row to `inactive`, both guards return 403 on the same request.
- **`org_role` is never read by a guard** — only inside `_ensure_admin_provisioned`, for the one-time create and the downgrade sync.

---

## Folder / file layout

```
app/
  api/admin/
    __init__.py             # APIRouter(prefix="/admin", tags=["admin"]); includes sub-routers; exports `router`
    context.py              # GET /context                                 (get_admin_db)
    invitations.py          # POST/GET/DELETE /invitations                 (guard: require_org_admin)
    members.py              # GET/DELETE /members                          (guard: require_org_admin)
    organizations.py        # POST/GET /organizations                      (guard: require_platform_admin)
    platform_invitations.py # POST /organizations/{clerk_org_id}/invitations (guard: require_platform_admin)  # NEW (D2)
  schemas/admin/
    __init__.py
    context.py              # AdminContextResponse, OrgSummary
    invitations.py          # CreateInvitationRequest, InvitationResponse   (reused by platform invite)
    members.py              # MemberResponse
    organizations.py        # CreateOrganizationRequest, OrganizationResponse, CreateOrgResult
  services/admin/
    __init__.py
    clerk_admin.py          # Clerk Backend API adapter (all raw httpx here; incl. fetch_clerk_user)
  core/
    admin_dependencies.py   # get_admin_db, _ensure_org_provisioned, _ensure_admin_provisioned,
                            #   require_org_admin, require_platform_admin
  models/
    clerk_admin_user.py     # NEW — ClerkAdminUser model + AdminType enum
  repo/
    AdminUserRepo.py        # NEW — get_by_clerk_id, upsert, deactivate
alembic/versions/
    <rev>_clerk_admin_users.py   # NEW — the ONE new migration
```

- `app/main.py` gains one import + one `include_router` line.
- `platform_invitations.py` reuses `CreateInvitationRequest`/`InvitationResponse` from `schemas/admin/invitations.py` (same body/response shape as `POST /admin/invitations`; only the target org and guard differ). A separate module keeps the platform-guarded cross-tenant path visibly distinct from the org-admin path.
- Admin code imports only: `get_claims`, `get_settings`, ORM models (`Organisation`, `Users`, `OrgType`, `ClerkAdminUser`, `AdminType`), `UserRepo` (read-only, members), `AdminUserRepo`, `HumanAuditRepo`, `CamelModel`, exceptions, and `AsyncSessionLocal`/`text`/`select`/`pg_insert`/`fetch_clerk_organization` for `get_admin_db`. **Not** product business logic.
- **`AdminUserRepo`** mirrors `UserRepo`:
  - `get_by_clerk_id(clerk_user_id)` → `select(ClerkAdminUser).where(clerk_user_id == ...)`.
  - `upsert(data)` → `pg_insert(ClerkAdminUser).values(**data).on_conflict_do_nothing(index_elements=["clerk_user_id"])`.
  - **`deactivate(clerk_user_id)`** *(NEW — D3)* → `update(ClerkAdminUser).where(clerk_user_id == ...).values(status="inactive")`. Revoke-only; there is no re-activate method. RLS scopes the UPDATE to the caller's own org.

---

## Clerk Backend API adapter (`app/services/admin/clerk_admin.py`)

Raw `httpx`, mirroring `fetch_clerk_organization` exactly. **SDK decision unchanged: raw `httpx`, not `clerk-backend-api`** — consistency, ~6 endpoints, no new dependency in a security-sensitive path, trivially mockable. Confine every Clerk call here.

| Adapter fn | Clerk call | Required fields |
|---|---|---|
| `create_organization(name, org_type, created_by)` | `POST /organizations` | `name`, **`created_by`** (see R1), `public_metadata={"type": org_type.value}` if `org_type` |
| `list_organizations(limit, offset)` | `GET /organizations?limit=&offset=` | limit≤500; paginate; caller filters out platform org |
| `create_organization_invitation(org_id, email, role, redirect_url, inviter_user_id=None)` | `POST /organizations/{org_id}/invitations` | `email_address`, `role` (`"org:member"`/`"org:admin"`), **`redirect_url`** (required param — no hardcoded default; D1), optional `inviter_user_id` |
| `list_organization_invitations(org_id, status="pending")` | `GET /organizations/{org_id}/invitations?status=pending&limit=&offset=` | — |
| `revoke_organization_invitation(org_id, invitation_id, requesting_user_id)` | `POST /organizations/{org_id}/invitations/{invitation_id}/revoke` | **`requesting_user_id`** (required) |
| `remove_organization_membership(org_id, member_user_id)` | `DELETE /organizations/{org_id}/memberships/{member_user_id}` | *(verify exact path against Clerk memberships API in Phase 2/4)* |
| **`fetch_clerk_user(clerk_user_id)`** | `GET /users/{user_id}` | returns `email_addresses` + `primary_email_address_id`; helper `fetch_clerk_user_primary_email` extracts the primary address string (or `None`) |

Adapter behavior:
- **Roles:** adapter accepts already-prefixed Clerk keys; endpoints decide which. Org-admin invitations and platform member-invites always `"org:member"`; org creation always `"org:admin"`.
- **`redirect_url` is a required per-invitation parameter (D1)** — the adapter no longer hardcodes `/sign-up`. Callers pass:
  - `POST /admin/organizations` (seed `org:admin`): `f"{settings.app_base_url}/admin/sign-up"`.
  - `POST /admin/invitations` (`org:member`): `f"{settings.app_base_url}/sign-up"`.
  - `POST /admin/organizations/{clerk_org_id}/invitations` (`org:member`, D2): `f"{settings.app_base_url}/sign-up"`.
- **`inviter_user_id`/`requesting_user_id`** = `claims["user_id"]`. (For D2, this is the platform admin's Clerk user id — Clerk accepts an inviter who is a member of the platform org; if Clerk rejects a cross-org inviter, omit `inviter_user_id` for this call and rely on the secret-key auth. Verify in Phase 5.)
- **Timeouts:** `5.0s`. On `httpx.HTTPError` → **502/503**. On Clerk 4xx → **409/422/429** with Clerk's message in `detail`. A Clerk 404 on `fetch_clerk_organization` for the D2 path param → **404**. Never leak the secret key or full payloads.
- Invitation creation is rate-limited (~250/hr) — surface 429s, don't retry blindly.

---

## Config additions (`app/core/config.py`)

```python
simpero_platform_org_id: str = (
    ""  # Clerk org id of the Simpero platform org; "" => platform surface denies all (fail closed)
)
app_base_url: str = "http://localhost:3000"  # builds invitation redirect_url(s)
```

- `simpero_platform_org_id` defaults to `""`; `require_platform_admin` fails closed on empty.
- **Both invitation redirect targets derive from `app_base_url` (D1):**
  - admin seed invite → `f"{app_base_url}/admin/sign-up"`
  - product-user invite (org-admin and platform paths) → `f"{app_base_url}/sign-up"`
  Build these in the endpoints (or a tiny `_redirect_for(role)` helper); do not hardcode the paths in the adapter. `/admin/sign-up` must be a real frontend route (cross-repo note — the frontend team owns it; flag if it is not yet present).
- Document both settings in `.env.example`; set the real platform org id per env. `clerk_secret_key` reused (also powers `fetch_clerk_user`).

---

## Audit logging (updated actor resolution)

Every admin **mutation** writes exactly one `HumanAuditRepo.append(...)` row inside the same `get_admin_db` transaction. Resolve the actor from the `clerk_admin_users` row, not `UserRepo`:

```python
async def _admin_actor(db, claims) -> tuple[int, str, str | None]:
    row = await AdminUserRepo(db).get_by_clerk_id(claims["user_id"])
    assert row is not None  # provisioned by get_admin_db; guard confirmed active
    return row.org_id, claims["user_id"], row.email
```

| Endpoint | `event_type` | `payload` |
|---|---|---|
| `POST /admin/invitations` | `admin_invitation_created` | `{ email, role: "member", clerk_invitation_id }` |
| `DELETE /admin/invitations/{id}` | `admin_invitation_revoked` | `{ clerk_invitation_id }` |
| `DELETE /admin/members/{id}` | `admin_member_removed` | `{ removed_clerk_user_id, removed_email }` |
| `POST /admin/organizations` | `admin_organization_created` | `{ clerk_org_id, name, type, account_manager_email, seed_invitation_id }` |
| `POST /admin/organizations/{clerk_org_id}/invitations` *(NEW — D2)* | `admin_member_invited_by_platform` | `{ target_clerk_org_id, email, role: "member", clerk_invitation_id }` |

- **Platform-admin actions are audited in the Simpero org's own trail.** The session is clamped to Simpero, `_admin_actor` returns the Simpero `organisation.id`, and the row satisfies the `human_audit_log` org-isolation `WITH CHECK`. **For D2 specifically:** the platform admin has no `clerk_admin_users` row in the client org and `get_admin_db` clamps to their own (Simpero) org, so the audit row is written **in Simpero's trail** with the client org captured in the payload (`target_clerk_org_id`) — not in the client org's trail. This is correct and unavoidable given the RLS clamp; the Clerk invitation still targets the client org via the API.
- `GET` endpoints write no audit rows. Never add update/delete to `HumanAuditRepo`.

**Member-removal semantics:** primary = revoke the member's Clerk org membership (`remove_organization_membership`); secondary = delete the local `users` row (RLS-scoped `DELETE`, JIT-rebuildable). Reversible by re-inviting. **Self-removal guard:** compare the *target* `users.clerk_user_id` (looked up by the path param) to `claims["user_id"]` → 403 `"Cannot remove yourself"` (the caller has no `users` row, so don't assume they appear in `users`). No soft-delete column (see R2).

---

## Phased implementation plan

Migration + table + `get_admin_db` land early. Phases 0–5 have no live-Clerk dependency (Clerk mocked) and can proceed while the frontend builds in parallel.

### Phase 0 — Migration, model, repo, config
- Add `simpero_platform_org_id` + `app_base_url` to `Settings`; update `.env.example`.
- Create `app/models/clerk_admin_user.py`; register with Alembic `Base.metadata`.
- Author the migration (`down_revision` = current head): table + `admintype` enum + indexes + `ENABLE ROW LEVEL SECURITY` + `org_isolation` policy. No `FORCE`/`GRANT`/`REVOKE`.
- Create `app/repo/AdminUserRepo.py` with `get_by_clerk_id`, `upsert`, **and `deactivate`** (D3).
**Acceptance:** `alembic upgrade head` then `downgrade -1` clean (type dropped); `ruff`/`pyright` clean. **DB-backed RLS check:** clamped to org A, `dd_app` can INSERT/SELECT an org-A row; an org-B row seeded via `owner_conn` is invisible; INSERT of an org-B row under an org-A clamp is rejected; `deactivate` under an org-A clamp cannot flip an org-B row (no rows matched).

### Phase 1 — `admin_dependencies` + Clerk adapter + schemas + router + `GET /admin/context` + R6 downgrade sync
- `admin_dependencies.py`: `get_admin_db` (SET LOCAL first; `_ensure_org_provisioned`; `_ensure_admin_provisioned` **with the D3 downgrade-only sync — restructured so the sync runs even when a row already exists**), `require_org_admin`, `require_platform_admin`.
- `clerk_admin.py`: all adapter fns incl. `fetch_clerk_user`; `create_organization_invitation` takes required `redirect_url` (D1).
- `schemas/admin/*` (extend `CamelModel`).
- `api/admin/__init__.py`; `GET /admin/context`. One `include_router` + import in `main.py`.
**Acceptance:**
  - `GET /api/admin/context` correct for platform-admin / client-admin / member tokens; camelCase; platform org id never in body.
  - **JIT test:** first-time admin token creates a `clerk_admin_users` row (correct `admin_type`) and **no** `users` row; member token creates no admin row and `require_org_admin` → 403. Clerk mocked.
  - **R6 downgrade sync (DB-backed, D3):** seed an **active** `client` admin row for org A via `owner_conn`; present a token for the same `clerk_user_id` with `org_role="member"`; assert (1) the row is flipped to `status="inactive"` in the same request, and (2) `require_org_admin`/`require_platform_admin` → **403** on that request. Conversely: an active row + `org_role="admin"` token stays `active`; an **inactive** row + `org_role="admin"` token stays `inactive` (never re-activated).

### Phase 2 — Org-admin members (`GET`/`DELETE /admin/members`)
- Guard `require_org_admin`. Member-removal semantics + self-removal guard.
**Acceptance:** list returns only caller-org users; delete writes `admin_member_removed` + removes the local row; self-removal → 403; member token → 403. Clerk revoke mocked.

### Phase 3 — Org-admin invitations (`POST`/`GET`/`DELETE /admin/invitations`)
- `POST` forces `org:member`, builds member `redirect_url = f"{app_base_url}/sign-up"` (D1); `role != "member"` → 422/403. Audit on create + revoke.
**Acceptance:** `POST role="admin"` rejected with no Clerk call; success calls the adapter with `role="org:member"` + `redirect_url = <app_base_url>/sign-up` and writes `admin_invitation_created`; `GET` lists pending; `DELETE` writes `admin_invitation_revoked`. Clerk mocked.

### Phase 4 — Platform-admin organizations (`POST`/`GET /admin/organizations`)
- `POST`: create org in Clerk, seed `org:admin` invitation w/ `redirect_url = f"{app_base_url}/admin/sign-up"` (D1); no local DB insert; audit `admin_organization_created`; handle `created_by` (R1). `GET`: list from Clerk, exclude platform org. Guard `require_platform_admin`.
**Acceptance:** non-platform-admin → 403; success makes the expected Clerk calls in order (seed invitation carries the **admin** `redirect_url`) and inserts **no** `organisation` row (assert via `owner_conn`); `GET` omits the platform org; R1 handling implemented + audited. Clerk mocked.

### Phase 5 — Platform member-invite into a client org (`POST /admin/organizations/{clerk_org_id}/invitations`) — NEW (D2)
- New `app/api/admin/platform_invitations.py`; guard `require_platform_admin`; reuse `CreateInvitationRequest`/`InvitationResponse`.
- Logic order: (1) guard; (2) reject `{clerk_org_id} == settings.simpero_platform_org_id` → 403; (3) `fetch_clerk_organization({clerk_org_id})` to validate existence (404 → 404); (4) `create_organization_invitation(org_id={clerk_org_id}, email, role="org:member", redirect_url=f"{app_base_url}/sign-up", inviter_user_id=claims["user_id"])`; (5) audit `admin_member_invited_by_platform` in the **Simpero** trail via `_admin_actor` (payload carries `target_clerk_org_id`).
- The session (`get_admin_db`) stays clamped to the Simpero org; no DB write ever targets the client org — see the RLS/session note.
**Acceptance:**
  - Non-platform-admin token → 403 with no Clerk call.
  - `{clerk_org_id} == simpero_platform_org_id` → 403 with no invitation call.
  - Non-existent `{clerk_org_id}` (mocked Clerk 404 on `fetch_clerk_organization`) → 404 with no invitation call.
  - Success: adapter called with `org_id={clerk_org_id}`, `role="org:member"`, `redirect_url=<app_base_url>/sign-up`; response `{ id, emailAddress, status, createdAt }`; exactly one `admin_member_invited_by_platform` row in the **Simpero** org's `human_audit_log` (assert via `owner_conn`; verify the client org's trail gets **no** row and payload carries `target_clerk_org_id`).
  - `role != "member"` → 422/403. Clerk mocked.

### Phase 6 — Tests + docs
- **Guard tests (table-authoritative):** seeded active row passes; `status != active` / no row / wrong `admin_type` / `tenant_id != platform_org` → 403; empty `simpero_platform_org_id` → 403. DB via `db_session`/`owner_conn`.
- **JIT test:** first admin request materializes the row, no product `users` row; email backfilled from mocked `fetch_clerk_user`; Clerk-unreachable → row with `email=None`, request succeeds.
- **R6 downgrade sync test:** as in Phase 1 acceptance, exercised end-to-end through a guarded endpoint (active row + member JWT → row flipped → endpoint 403).
- **RLS caveat:** don't unit-test isolation with a mocked DB. Seed a second org's `clerk_admin_users` + `users` via `owner_conn`; assert `GET /admin/members` never returns them and the guard lookup never returns the second org's admin row.
- Contract tests per endpoint via `ApiTestClient` (override `get_claims`, `monkeypatch` clerk_admin fns), including the D2 platform member-invite path.
- Update `.env.example` + README with the two settings, the one-time "grant a human `org:admin` in the Simpero Clerk org" bootstrap step, and the D1 redirect-route note (`/admin/sign-up` must exist on the frontend).
**Acceptance:** `pytest` green with Postgres; `ruff`/`pyright` clean.

---

## BACKLOG (out-of-scope-for-now): admin user that is ALSO a product user

Today a client admin is **admin-only** — no product `users` row. To later allow an admin to *also* be a product user:
- **Option A — opt-in dual provisioning:** a flag (e.g. `also_product_user`) that makes a future request additionally run `_ensure_user_provisioned`, so the person gets a product `users` row in the same org. Rows coexist keyed on the same `clerk_user_id`; audit-actor resolution already resolves independently per route.
- **Option B — link column:** `clerk_admin_users.product_user_id` (nullable FK → `users.id`).
Either is a new migration + provisioning change; deferred. `email` is intentionally non-unique today so the same human can appear in both tables.

Also deferred: a full admin-management endpoint (`DELETE/PATCH /admin/admins`) that can **re-activate** a demoted-then-re-promoted admin. The D3 sync deliberately only revokes; re-activation is not automatic.

---

## Out of scope / do not touch

- **All product routers/services** (`deals`, `history`, `investment_profile`, `logs`, `health`, `auth`; `dashboard_stats`, `memo_summary`, `pipeline_steps`). Reuse only `get_claims`, `UserRepo` (read-only, members), `HumanAuditRepo`, models, `CamelModel`.
- **Existing RLS policies + product tables:** no changes to `aace95a1c412`, `c9aaf2c46b16`, `7175bc85ffb0`, `bootstrap_dd_app_privliges.py`, or any product table. **The ONLY new migration is `clerk_admin_users`.** No RLS-bypass role.
- **`_ensure_user_provisioned` / `get_db` / `security.py`:** read-only shared primitives; `get_admin_db` is a NEW function — do not modify `get_db`.
- **`HumanAuditRepo`:** never add update/delete.
- **The parsing pipeline / MNPI boundary:** untouched; flag if any need arises.
- **The frontend repo** (`Simpero_AI_Gov_Web`): frontend team owns it. D1 depends on a new `/admin/sign-up` route existing there — cross-repo dependency, flag at handoff.

---

## Open questions / risks for a human

- **R1 — Clerk org creation requires `created_by` (STILL OPEN).** `POST /organizations` requires a `created_by` user id, and that user becomes an **admin MEMBER of the new client org in Clerk** — letting a platform admin switch their active org to the client and read the client's **product** data through RLS. This is a *Clerk-membership-level* hole, not addressed by `clerk_admin_users`. **Recommended default: remove-after-create** (immediately `remove_organization_membership(new_org_id, platform_admin_user_id)`). Alternative: a dedicated Clerk service/bot user as `created_by`, removed the same way. Not yet decided. (Also verify current Clerk API version: if `created_by` can be omitted, omit it and drop the removal step.)
- **R2 — Member-removal reversibility.** We delete the JIT-rebuildable `users` row rather than soft-flag (avoids a second migration). A durable `is_active` soft-delete would need its own migration — out-of-scope; confirm preference.
- **R3 — Last-admin / org-lockout protection.** Should `DELETE /admin/members` refuse to remove the org's last admin (beyond self-removal)? Recommended, but a product decision.
- **R4 — New settings in deployed envs.** `simpero_platform_org_id` fails closed until each env sets the real Clerk platform org id. Confirm it exists and is available. `app_base_url` must point at the deployed frontend (drives both D1 redirect paths).
- **R5 — Migration role note.** `bootstrap_dd_app_privliges.py` runs `ALTER DEFAULT PRIVILEGES FOR ROLE doadmin`, not `dd_owner`. The new table relies on that block to auto-grant `dd_app` DML. If the DDL role switches to `dd_owner` before this ships, a matching `ALTER DEFAULT PRIVILEGES FOR ROLE dd_owner` must exist first, else admin requests fail closed. Align separately.
- **R6 — Table-authz staleness / de-authorization (BUILT — downgrade-only, D3).** Guards authorize off `clerk_admin_users`, not the live JWT, so a Clerk demotion would otherwise leave portal access intact. **Now implemented:** `_ensure_admin_provisioned` runs a downgrade-only reconciliation — an existing **active** row whose caller's JWT `org_role != "admin"` is set to `status='inactive'` on the next request; the guard then returns 403. It **only revokes** (JWT trusted for de-authorization only — the safe direction). Two known, accepted limits: (a) revocation lags by one request (until the demoted user next hits an admin route); (b) it does **not** re-activate a re-promoted admin — that needs a future `DELETE/PATCH /admin/admins` endpoint (BACKLOG). Phased in Phase 1 with DB-backed acceptance.

**Sources:**
- Clerk — create organization invitation (incl. `redirect_url`): https://clerk.com/docs/reference/backend/organization/create-organization-invitation
- Clerk — get user (email backfill): https://clerk.com/docs/reference/backend/user/get-user
