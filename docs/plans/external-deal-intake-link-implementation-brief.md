# External Deal Intake Link — Implementation Brief (Phase P1, Alpha repo)

**Audience: a Claude Code CLI session running locally against a checkout of `Simpero_AI_Gov_Alpha`, with normal git/network/file access.**
**Author: a Cowork planning session that spent two rounds of review (2026-08-24, 2026-08-25) with Vansh and the PO. No code has been written yet — this document is the full, current, decided spec.**

This is the only context you get. Read this whole document before writing any code. It is written so that implementing it faithfully requires no re-derivation, no re-litigating decisions, and no guessing — every open question that remains open is called out explicitly as "OPEN" below, and everything else is settled. If you find yourself about to make a design call this document doesn't cover, stop and flag it back to Vansh rather than picking one silently — see "Operating rules" below.

---

## 0. Operating rules (read first)

1. **Scope for this pass: Phase P1 only** — the `dd_public` role, the three migrations, both keyhole policies, the `dd_public` grant matrix and its policy twins, the second connection pool, both public dependency functions, the `BYPASSRLS`/superuser proof, the exact-grant-list drift test, and the cross-tenant negative test suite. **No routes, no frontend.** P1 is deliberately provable in isolation before anything is built on top of it — do not jump ahead to P2/P3 tickets even if they look easy.
2. **Implement one ticket at a time**, in the dependency order given in section 6. After each ticket: run its acceptance criteria against a **real Postgres instance** (RLS cannot be exercised against SQLite — this repo's own CI already knows this; see section 5.6). Do not mark a ticket done, and do not move to the next one, until its acceptance criteria pass against real Postgres.
3. **Update ticket status as you go**, in `docs/plans/external-deal-intake-link-status.md` (create it — template in section 7). Do not create a new tracking spreadsheet or file elsewhere. This file is the single source of truth Vansh will read to know what happened; keep it current after every ticket, not just at the end.
4. **Do not deviate from this spec without flagging it back to Vansh first.** If something in this document turns out to be wrong once you're looking at the real code (a function that doesn't exist where described, a column that's named differently, a convention that's changed since 2026-08-25), stop, write down exactly what you found and why it conflicts, and surface it — in the status file under a "Flagged" section, and in your final summary to Vansh — rather than silently adapting the design to fit. This document was built from a real repo read, so drift should be rare, but it is not impossible.
5. **Two things in this document are explicitly undecided.** Do not resolve them yourself:
   - **OPEN-1**: whether `dd_public`'s `CREATE ROLE` belongs in an Alembic migration or should follow the `dd_app` precedent of a plain SQL init script. See section 4.7 for the full analysis and a recommended default — but confirm with Vansh before P1-00 if there's any doubt, since this affects how the role gets created in production.
   - **OPEN-2 (out of scope for P1 entirely)**: malware scanning on public uploads (Q17). Do not build it, do not stub it, do not add a TODO route for it. It belongs to P3 and is not decided yet.
6. **Commit as you go.** One commit per ticket (or a small cluster of tightly-coupled tickets, e.g. P1-08/P1-09 alongside P1-00 if that's more natural), with a message referencing the ticket ID. Do not squash P1 into one giant commit — Vansh and the PO will want to review it ticket by ticket.
7. **This feature's core property, stated once so it's never lost in implementation detail**: an external, unauthenticated party can write to this database and upload documents into it. Every line of P1 exists to make sure that party can touch exactly five tables, exactly the rows belonging to one deal, and nothing else — provably, at the database level, not just by code review. If a test in section 6 feels excessive, it isn't; that's the point of doing P1 before anything else.

---

## 1. Feature summary (context, not build scope for P1)

Today, the New Deal wizard requires the org user to upload all diligence documents themselves. This feature adds a second path: the org user can generate a shareable link instead, which lets an external party (e.g. the company being diligenced) submit answers to a fixed question set and upload documents themselves — without logging in. The deal only becomes ready to route to Step 3 / analysis once that external party explicitly submits.

Full flow (for orientation — none of this is P1's build scope):

- **Org side**: Step 1 gains a checkbox + recipient email. Checked → generate a link, show it once, org user copies and sends it themselves (no transactional email in scope). Step 2 becomes a waiting panel. Step 3 shows the external party's answers plus a real per-document status list.
- **External side**: no login. Open the link → enter email (must match the nominated recipient) → answer a fixed set of platform-defined questions (free text) → upload documents via the same presign/PUT/complete pipeline the product already uses → explicit Submit, which is unrepeatable and closes the link.
- **Everything downstream of Start Analysis is unchanged.** A document uploaded externally becomes an ordinary `data_source` row, ingested and parsed exactly like an org-uploaded one. `Simpero_Gov_AI_Services` needs zero changes for v1.

The hard part, and the reason P1 exists as its own phase: an external request carries no Clerk JWT, so there's no `org_id` claim, so naive RLS denies everything — including the very link row you'd need to read to discover which org the token belongs to. Section 4 is the resolution to that, agreed and reviewed twice.

---

## 2. Data model

Three new tables. Read this section fully before writing P1-00/01/02 — the columns, the RLS posture, and the *reasons* for each design choice all matter for getting the migrations right.

### 2.1 `deal_intake_questions` — global reference, **no RLS** (P2, not P1 — included here for context only)

Mirrors `mandate_categories`/`mandate_options` exactly: no `org_id`, no RLS, read-only from the product portal, full CRUD from the admin portal restricted to platform admins.

Columns: `id`, `question_key` (immutable slug, same pattern as `mandate_categories.slug` with its partial unique index), `prompt`, `help_text`, `display_order`, `input_type`, `required`, `is_active`, timestamps.

**Design point — why the link snapshots the question set at generation time**: a global table that platform admins edit means wording can change between a link being sent and an answer arriving. The link row therefore carries a `questions_snapshot` JSONB of the exact prompts served at generation time, and the response stores answers against that snapshot — same problem the `mandates` blob solves by storing option text alongside option ids.

### 2.2 `deal_intake_link` — tenant-scoped, **RLS + FORCE** (P1-01)

Columns: `id`, `org_id`, `clerk_org_id`, `deal_id`, `token_hash`, `recipient_email`, `questions_snapshot` (JSONB), `status`, `expires_at`, `created_by_user_id`, `submitted_at`, `failed_attempts`, `last_attempt_at`, `created_at`.

- The raw token is generated with `secrets.token_urlsafe(32)`, returned exactly once in the create response, and **never stored** — only its SHA-256 (`token_hash`). Same posture as `storage_key`: derived and verified server-side, never round-tripped through the client as a trusted value.
- **`clerk_org_id` is a denormalized copy of `organisation.clerk_org_id`, written once at insert.** It exists only so phase 2 of the public RLS handshake (section 4) can read the tenant id off the row the keyhole policy already exposed, without a second, un-guarded join into a table that is itself RLS'd. It is never updated after insert.
- Status is one-way: `pending → submitted | revoked | expired`, enforced by a `BEFORE UPDATE` trigger and a narrow column grant — the exact `data_source` idiom: `REVOKE UPDATE, DELETE`, then `GRANT UPDATE (status, submitted_at, failed_attempts, last_attempt_at)`. Triggers fire for every role including the table owner (`doadmin`), which is what makes "a submitted link can never be reopened" actually true, not merely intended.
- **Nothing writes `expired` on a schedule** — there is no cron anywhere in this app, confirmed by repo read, and adding one for a single column flip isn't worth the infra. Expiry is **lazy**: every read path (link status, the keyhole policies, the dashboard's future `intakeStatus`) computes an **effective status** through one shared helper — `status` as stored, except `pending` with `expires_at <= now()` reads as `expired`. Link generation (`POST /deals/{deal_id}/intake-link`, P3, not P1) additionally performs the real `UPDATE ... SET status = 'expired'` write, lazily, the moment it finds an existing row that is `pending` but past `expires_at` — exactly the moment a reissue needs that row out of the way. **For P1, this means:** the keyhole policies' `USING` clauses already encode "pending AND not expired" (see section 4.2) — that's the enforcement point; you do not need a scheduled job or a background writer as part of P1.
- Partial unique index on `(deal_id) WHERE status = 'pending'` — one live link per deal. The lazy-expire-on-generate step (P3) is what keeps this index from blocking a reissue; not P1's concern, but know why the index is shaped this way.

### 2.3 `deal_intake_response` — tenant-scoped, **RLS + FORCE, append-only** (P1-02)

Columns: `id`, `org_id`, `deal_id`, `link_id`, `respondent_email`, `answers` (JSONB), `submitted_at`, `ip_address` (INET), `user_agent`, `created_at`.

Blanket `REVOKE UPDATE, DELETE` — the `human_audit_log` idiom. An answer submitted by an outside party is a historical fact, not editable state. `deal_id` is denormalized alongside `link_id` so the org-side read is one indexed lookup.

This is the table the original requirement describes: answers as a JSON object, keyed by the external user's email and the deal id the link was generated for.

### 2.4 Stored JSON shapes (for context — not written by P1, but the columns exist in P1's migrations)

`questions_snapshot` (captured once, at link generation):
```json
{
  "snapshot_version": 1,
  "captured_at": "2026-08-24T11:02:17.441Z",
  "questions": [
    {
      "question_key": "use_of_proceeds",
      "prompt": "What are the proceeds of this raise being used for?",
      "help_text": "A summary is fine — we will follow up on specifics.",
      "input_type": "textarea",
      "required": true,
      "display_order": 10
    }
  ]
}
```

`answers` (written once, at Submit, into the append-only response table):
```json
{
  "schema_version": 1,
  "answers": [
    {
      "question_key": "use_of_proceeds",
      "prompt": "What are the proceeds of this raise being used for?",
      "answer": "Roughly 60% to expand the Toronto engineering team, the rest to 18 months of runway.",
      "answered": true
    }
  ]
}
```
Both stored blobs are snake_case, consistent with `mandates.mandate` / `screening_result.rule_results` in this codebase — `CamelModel`'s `to_camel` alias generator + `populate_by_name=True` is what handles the camelCase conversion on the wire. This is P3 scope; noted here only so P1's JSONB columns aren't second-guessed later.

---

## 3. The chicken-and-egg problem, and why P1 exists

An external request carries no Clerk JWT → no `org_id` claim → `app.org_id` cannot be set → RLS denies everything, including the `deal_intake_link` row you'd need to read in order to discover which org the token belongs to. Everything in section 4 is the resolution.

**The property that makes the resolution defensible: tenant scope is derived from the database, never asserted by the caller.** There is no request field an attacker can change to point at another org — the only input to either public dependency is a secret (link token, or session JWT) that must match a live row.

---

## 4. The RLS handshake (the core of P1)

### 4.1 Why two dependencies, not one

The raw link token (7-day TTL, travels by email) and the intake session token (30-minute TTL, signed with its own key, never leaves the browser tab after issuance) are different secrets carrying different weight. An earlier draft of this plan used one `get_public_db(token)` dependency for everything; PO review on 2026-08-25 correctly flagged that this blurs the distinction and leaves it unclear what the session token buys you. **Resolution: two dependencies, two keyhole policies, one dependency used exactly once.**

### 4.2 The two keyhole policies (P1-03)

```sql
-- Migration: TWO policies on deal_intake_link, alongside org_isolation.
-- Each is a keyhole, not a door — it exposes exactly one row, and only
-- to a caller who already holds the matching secret. Neither can
-- enumerate, list, or see a link that is expired, revoked or submitted.
--
-- Both are granted TO dd_public — NOT TO dd_app. The authenticated
-- product surface reaches links through org_isolation and never needs
-- a keyhole, so the keyhole does not exist for it.

-- Policy A: for POST /session only. Keyed on the long-lived link token
-- the recipient actually holds.
CREATE POLICY intake_token_lookup ON deal_intake_link
    FOR SELECT TO dd_public
    USING (
        token_hash  = current_setting('app.intake_token_hash', true)
        AND status  = 'pending'
        AND expires_at > now()
    );

-- Policy B: for every route AFTER /session. Keyed on the link's own id,
-- which only ends up in app.intake_link_id after the session JWT has
-- been verified. The raw token never appears again past this point.
CREATE POLICY intake_session_lookup ON deal_intake_link
    FOR SELECT TO dd_public
    USING (
        id          = current_setting('app.intake_link_id', true)::uuid
        AND status  = 'pending'
        AND expires_at > now()
    );
```

Plus, on the same table, a `dd_public` `UPDATE` policy with a `WITH CHECK` restricting the status values the public role may write (it may move `pending → submitted`, stamp `failed_attempts`/`last_attempt_at`; it may not touch anything else).

### 4.3 The two public dependency functions (P1-04, P1-06)

New module: `app/core/public_dependencies.py`. **Never imported by `app/core/dependencies.py`. Never imported into any admin or product router.** Runs on `PublicAsyncSessionLocal` — a *separate* engine bound to `dd_public` (section 4.5) — never the `dd_app` pool.

```python
# app/core/public_dependencies.py — never imported by app/core/dependencies.py
# PublicAsyncSessionLocal is a SEPARATE engine bound to the dd_public role.
# app/core/dependencies.py keeps AsyncSessionLocal on dd_app; the two pools
# never mix, and no other module imports this one.
# Used by exactly one route: POST /api/public/intake/{token}/session.
async def get_public_link_db(token: str) -> AsyncGenerator[AsyncSession, None]:
    async with PublicAsyncSessionLocal() as session, session.begin():
        # Phase 1. FIRST statement in the transaction. The only thing
        # visible at this point is one link row, via keyhole policy A.
        await session.execute(
            text("SELECT set_config('app.intake_token_hash', :h, true)"),
            {"h": sha256_hex(token)},
        )
        link = await IntakeLinkRepo(session).get_by_token_hash(sha256_hex(token))
        if link is None:
            raise HTTPException(404, "Not found")   # never 403 — see section 5.2

        # Phase 2. org_id comes off the link row itself — clerk_org_id is a
        # denormalized column on deal_intake_link (section 2.2), NOT a join
        # through `organisation`. organisation is RLS'd on the same
        # clerk_org_id, so reading link.organisation.clerk_org_id here
        # would fail: app.org_id isn't set yet, and organisation's own
        # org_isolation policy would return nothing. Same "SET LOCAL must
        # be the FIRST statement" ordering get_db already depends on.
        #
        # app.intake_deal_id goes up alongside it, so the dd_public
        # policies on data_source can scope to ONE deal rather than the
        # whole org — see section 4.4.
        await session.execute(
            text("""SELECT set_config('app.org_id', :tid, true),
                           set_config('app.intake_deal_id', :did, true)"""),
            {"tid": link.clerk_org_id, "did": str(link.deal_id)},
        )
        yield session, link

# Used by every OTHER public route: /questions, /answers, /uploads/*, /submit.
async def get_public_session_db(session_token: str) -> AsyncGenerator[AsyncSession, None]:
    claims = decode_intake_session_jwt(session_token)   # own signing key, own audience
    async with PublicAsyncSessionLocal() as session, session.begin():
        # Phase 1. FIRST statement. Keyhole policy B, keyed on link_id from
        # the verified session claim rather than a token hash — the raw
        # link token was already spent at /session and is never sent again.
        await session.execute(
            text("SELECT set_config('app.intake_link_id', :lid, true)"),
            {"lid": str(claims.link_id)},
        )
        link = await IntakeLinkRepo(session).get_by_id(claims.link_id)
        if link is None:
            raise HTTPException(404, "Not found")

        # Phase 2. Same clerk_org_id column, same reasoning as above.
        await session.execute(
            text("""SELECT set_config('app.org_id', :tid, true),
                           set_config('app.intake_deal_id', :did, true)"""),
            {"tid": link.clerk_org_id, "did": str(link.deal_id)},
        )
        yield session, link
```

`decode_intake_session_jwt` and the corresponding encode side are P3 scope (`P3-07`) — P1 only needs the **decode** function to exist with the right shape (own signing key/secret, own audience claim, `link_id` + `email` claims) so `get_public_session_db` can call it; stub or real implementation is your call as long as the audience-check property in section 5 holds and P1-05's tests can exercise it. If P3-07 hasn't been implemented yet when you reach P1-06, implement the minimal decode/encode pair needed to make P1-05 pass — do not leave `get_public_session_db` untestable.

### 4.4 The grant matrix (P1-00) — the exact and only privileges `dd_public` gets

| Table | `dd_public` holds | Why exactly that |
|---|---|---|
| `deal_intake_link` | SELECT; UPDATE (`status`, `submitted_at`, `failed_attempts`, `last_attempt_at`) | The handshake reads it; `/session` stamps failed attempts; `/submit` flips status. Never INSERT — only an org user creates a link. Never DELETE. |
| `deal_intake_response` | INSERT | Written once at Submit. Deliberately **no SELECT** — the external surface never reads answers back, not even its own. |
| `data_source` | SELECT, INSERT | The file-count ceiling and the ≥1-required check read it; `/complete` writes it. No UPDATE — the ingest worker runs as `dd_app`. |
| `organisation` | SELECT (`name`, `clerk_org_id` only) | The display name for the page and the storage-key prefix. Two columns of one row. |
| `human_audit_log` | INSERT | Every external action gets a row. Already append-only for every role. |
| `deal_intake_questions` | — nothing — | Questions come from the link's own `questions_snapshot`, never the global table. |
| `deals`, `mandates`, `screening_result`, `analysis_run`, `users`, everything else | — nothing — | Not granted. A query that reaches for one fails with `permission denied` — loudly, in development — instead of quietly returning rows. |

**Add the `dd_public` policy twins** on `data_source`, `organisation`, `deal_intake_response`, and `human_audit_log` — RLS applies policies by role, so a `dd_app`-targeted policy does nothing for `dd_public`. `data_source`'s SELECT policy for `dd_public` scopes on **both** `org_id = app.org_id` **and** `deal_id = app.intake_deal_id` (see 4.4a below). `deal_intake_response`'s INSERT policy carries a `WITH CHECK` binding every row to `app.intake_link_id`.

### 4.4a Intra-org scoping — the part that answers "can a public session see another deal in the same org"

Phase 2 of both dependencies sets a third transaction-local GUC alongside `app.org_id`: `app.intake_deal_id`, read off the same vouched-for link row.

```sql
CREATE POLICY intake_deal_documents ON data_source
    FOR SELECT TO dd_public
    USING (
        org_id       = current_setting('app.org_id', true)
        AND deal_id  = current_setting('app.intake_deal_id', true)::uuid
    );

-- And on the write side, a WITH CHECK binds every answer row to the
-- session's own link. A public session cannot write a response
-- attributed to a different link, even within its own org.
CREATE POLICY intake_response_insert ON deal_intake_response
    FOR INSERT TO dd_public
    WITH CHECK (
        org_id       = current_setting('app.org_id', true)
        AND link_id  = current_setting('app.intake_link_id', true)::uuid
    );
```

So a public session cannot see another deal's documents *inside its own org* — not because the router declines to ask, but because the database declines to serve it. This is what separates "intra-org scoping rests entirely on `public_intake.py` being correct" (rejected) from "intra-org scoping is a policy predicate" (built).

### 4.5 Why a separate connection pool and not `SET ROLE` (P1-07)

`dd_public` gets its **own login credential** and its **own engine** (`PublicAsyncSessionLocal`), imported by nothing but `app/core/public_dependencies.py`, using `poolclass=NullPool` — same reasoning as the existing `dd_app` engine: PgBouncer *is* the pool under transaction-mode pooling, so an app-side pool on top of it is the wrong layer.

**Deliberately NOT `SET LOCAL ROLE dd_public`** on the existing `dd_app` pool: `SET ROLE` requires `dd_app` to be a *member* of `dd_public`, and RLS applies a policy's `TO` clause by role **membership** — which would make the keyhole policies apply to ordinary authenticated product sessions too, quietly re-opening the door the role exists to shut.

**PgBouncer capacity — do not skip this, it was a PO-mandated pre-close confirmation, not a nice-to-have:**
- Today's `pgbouncer.ini` (in `docker/pgbouncer.ini` and referenced from the root `docker-compose.yml`) defines one role, `dd_app`, at `default_pool_size = 20`, under a global `max_client_conn = 22` — already tight for one caller.
- Adding `dd_public` via the wildcard `[databases] * = ...` entry would inherit that same `default_pool_size` and compete for the *same* 22-connection client budget as the product app and worker — a starvation risk for the very surface meant to be isolated.
- **Three concrete changes required, not one:**
  1. Give `dd_public` its own **named `[databases]` entry** with an explicit, deliberately smaller `pool_size`.
  2. **Raise `max_client_conn`** to give the added traffic path real headroom.
  3. **Confirm the underlying DigitalOcean Postgres cluster's own `max_connections`** has room for both pools' combined backend connections plus `doadmin`'s migration connections. This is plan-tier-dependent and not visible from the repo — check it directly in the DO console for both the staging cluster and whichever cluster production ends up on. **If you cannot access the DO console from this session, do not guess a number — flag it back to Vansh as a manual confirmation he needs to do, and note it as pending in the status file rather than marking P1-07 fully done.**
- New config entry for the `dd_public` credential (its own env var, following whatever convention `DATABASE_URL`/`ALEMBIC_DATABASE_URL` already use in this repo's `.env`/settings).

### 4.6 Three confirmations required before P1 closes (PO-mandated, 2026-08-25 — not optional)

These three were raised by the PO explicitly as required proofs, not objections to the design. P1 is not done until all three pass.

1. **`dd_public` cannot sidestep RLS — proven, not assumed (P1-08).** Two role-level properties bypass row-level security regardless of any policy or `FORCE` setting: the `BYPASSRLS` role attribute, and `superuser`. Table ownership is a third path, already closed by this codebase's existing `FORCE ROW LEVEL SECURITY` convention (which every RLS'd table already uses, and which P1-01/P1-02 must apply too) — `FORCE` is what makes RLS bind even against an owner. `dd_public` must never hold `BYPASSRLS`, never be superuser, and never own a table (DML-only, same shape as `dd_app`; only `doadmin` does DDL/ownership). Required test: query `pg_roles` for `dd_public`, assert `rolbypassrls = false AND rolsuper = false`. Required behavioral companion: a `dd_public` session with no GUC set still returns **zero rows** from a table it holds SELECT on — true only if neither attribute is set.

2. **The grant list is locked to an exact set — any future drift fails the build (P1-09).** P1-05's negative tests prove `dd_public` is denied on *today's* tables outside the grant matrix — they cannot catch a *future* widening, because a newly-granted table wouldn't be in the sample being checked. This is the allowlist counterpart: introspect `dd_public`'s actual privileges from `information_schema.table_privileges` and `column_privileges`, and assert the result equals the exact expected set — table by table, column by column, matching section 4.4's table exactly. A migration that adds any grant not in this hardcoded expected set must fail this test, by name, in CI.

3. **The second connection pool actually fits (folded into P1-07, section 4.5 above).** Not a code artifact — a verified fact about the actual DO cluster's `max_connections`, confirmed directly, not estimated.

---

## 5. Structural rules and security posture (context for P1, binding for later phases)

These apply to the *whole* feature; P1 only needs to keep them true where P1 touches (roles, policies, pools). Read them now so P1's foundations don't quietly violate something P3 depends on.

1. External routes (P3, not P1) will live in `app/api/public_intake.py`, using **only** `get_public_link_db` or `get_public_session_db` — never `get_db`, never both public dependencies on one route. That module will import nothing from `app/api/admin/**` and be imported by nothing — the same three-way separation `CLAUDE.md` already enforces between admin and product, extended to a third, weaker-trust surface.
2. **Every failure returns 404, never 403 or any other code that distinguishes cases.** Bad token, expired, revoked, already submitted, wrong email — same 404, same body. Distinguishing them turns the endpoint into an oracle for enumerating valid links and confirming recipient addresses. P1's dependency functions already follow this (see the `raise HTTPException(404, ...)` calls in section 4.3) — do not "improve" this to a more specific status code.
3. `deal_id` for uploads (P3) will be read off the link row, never the request body — same reasoning that makes `build_object_key` server-derived. Not P1's concern directly, but it's why `app.intake_deal_id` exists as a GUC at all (section 4.4a) — don't remove it thinking it's redundant.
4. **`dd_public`'s privileges are additive-by-exception.** A table added next year is unreachable from the public surface until someone deliberately grants it. The failure mode of forgetting is a loud error in development, never a quiet leak in production. P1-09 is what makes this an enforced property, not just a stated one.
5. **CORS is not a security control here.** The public endpoints (P3) will be reachable by any HTTP client regardless of `CORS_ALLOWED_ORIGINS` — the browser policy only stops other *web pages* reading responses. Not P1's concern, noted so it isn't rediscovered.
6. **Why not the alternatives** (for context, in case you're tempted): a `SECURITY DEFINER` function to look up the link creates a standing RLS bypass in the schema that outlives this feature. Leaving `deal_intake_link` outside RLS exposes every org's tokens/emails/deal-ids to any authenticated tenant — non-starter. "Discipline alone" (a small isolated module, no role) was the original alternative to the `dd_public` role and was explicitly rejected at product review in favor of the grant matrix — it's a single mechanism whose failure mode is silence, guarding the exact outcome flagged `[CRITICAL]` in the original requirement. Passing `orgId`/`dealId` in a URL and trusting it is an IDOR by construction.

### 5.1 Test infrastructure this repo already has

`.github/workflows/ci.yml` runs a real `pgvector/pgvector:pg16` Postgres service container specifically because RLS/`current_setting()` tests can't run on SQLite. `DATABASE_URL` uses `dd_app`; `ALEMBIC_DATABASE_URL` uses `doadmin`. Local dev uses `docker-compose.dev.yml` (Postgres on host port 5434, same `sandbox/init` role-creation scripts, no PgBouncer — direct connection, since `SET LOCAL` scoping works the same without the pooler). **Run P1's tests against this real Postgres setup, both locally and confirm they'd pass in CI** — do not write RLS tests that only run against SQLite or a mock.

### 4.7 OPEN-1 — how should `dd_public`'s `CREATE ROLE` actually be implemented?

Ticket P1-00 (section 6) is titled "Migration: dd_public Postgres role + grant matrix," which assumes Alembic for everything. But this repo's existing precedent for role creation is **not** an Alembic migration: `dd_app` is created by a plain SQL init script, `sandbox/init/01-app-role.sql` (`CREATE ROLE dd_app WITH LOGIN PASSWORD 'sandbox_dd_app';`), run by Docker Compose's init mechanism locally and referenced separately in CI's "Create dd_app role" step — **not** version-controlled as an Alembic migration that runs against production via `doadmin`.

This matters because Alembic migrations in this codebase run automatically against the live cluster as part of deploy; role creation with a real password is normally a one-time, out-of-band, secret-managed operation (this repo's own `docs/plans/do-droplet-deployment.md` confirms `.env` is generated fresh per deploy from GitHub Environment secrets, not committed).

**Recommended split** (default to this if you don't hear back from Vansh in time, but flag it in the status file regardless so it's a visible decision, not a silent one):
- **Role creation** (`CREATE ROLE dd_public WITH LOGIN PASSWORD '...'`) follows the `dd_app` precedent: add a sibling init script (e.g. `sandbox/init/02-public-role.sql`) for local/CI, and treat production role creation as a manual/secrets-managed step alongside however `dd_app`'s production credential is currently provisioned (check how that's actually done today — likely Terraform, a DO console action, or a documented manual runbook step; mirror whichever it is).
- **The grant matrix (all the `GRANT`/`REVOKE` statements)** *does* belong in Alembic, because grants attach to specific tables and columns that migrations already own, need to be reversible alongside schema changes, and are exactly what P1-09's drift test needs to be able to trace through migration history.

If your read of the repo turns up a different or more current convention than what's described above, that's exactly the kind of drift to flag per operating rule 4 — don't silently pick a side.

---

## 6. P1 tickets, in dependency order

Work through these in the order listed — it is the actual dependency order (each ticket's "Depends on" column is satisfied by everything above it). Do not reorder for convenience.

### P1-00 — Migration: `dd_public` Postgres role + grant matrix
**Priority P0. Depends on: nothing.**

Create a third Postgres login role, `dd_public`, alongside `doadmin` (DDL) and `dd_app` (product surface). Grant it exactly, and only: SELECT + UPDATE(`status`, `submitted_at`, `failed_attempts`, `last_attempt_at`) on `deal_intake_link`; INSERT on `deal_intake_response` (no SELECT); SELECT + INSERT on `data_source` (no UPDATE); SELECT(`name`, `clerk_org_id`) on `organisation`; INSERT on `human_audit_log`. No grant of any kind on `deal_intake_questions`, `deals`, `mandates`, `screening_result`, `analysis_run`, `users`, or any other tenant table. Add the `dd_public` policy twins on `data_source`, `organisation`, `deal_intake_response` and `human_audit_log` (section 4.4/4.4a). See section 4.7 (OPEN-1) for how role creation vs. grants should be split across Alembic and an init script.

**Acceptance criteria:** `dd_public` gets `permission denied` on `deals`, `mandates`, `screening_result`, `analysis_run`, `users` and `deal_intake_questions` — asserted table by table, not by sampling. A `dd_public` session scoped to deal A returns zero `data_source` rows for deal B in the same org. An INSERT into `deal_intake_response` naming a different `link_id` violates the `WITH CHECK`. Migration reverses cleanly, dropping the role last.

### P1-01 — Migration: `deal_intake_link` table
**Priority P0. Depends on: nothing (can run parallel to P1-00, but P1-03 needs both).**

Create `deal_intake_link` per section 2.2. Enable + FORCE row-level security with the standard `org_isolation` policy (same idiom as `data_source`). Add a `BEFORE UPDATE` trigger enforcing status is one-way (`pending → submitted | revoked | expired`), `REVOKE UPDATE/DELETE` from `dd_app`, then `GRANT UPDATE` on (`status`, `submitted_at`, `failed_attempts`, `last_attempt_at`) only. Add a partial unique index on `(deal_id) WHERE status = 'pending'`.

**Acceptance criteria:** Migration applies and reverses cleanly. A second UPDATE attempting to move `status` off a terminal value raises, even as `doadmin`. Two INSERTs for the same `deal_id` both `pending` violate the partial unique index. `clerk_org_id` is populated on every insert and is never null.

### P1-02 — Migration: `deal_intake_response` table
**Priority P0. Depends on: nothing.**

Create `deal_intake_response` per section 2.3. Enable + FORCE RLS with `org_isolation`. Blanket `REVOKE UPDATE, DELETE` from `dd_app` — append-only, same idiom as `human_audit_log`.

**Acceptance criteria:** Migration applies and reverses cleanly. Any UPDATE or DELETE from the app role fails at the database level, not just in application code.

### P1-07 — Public connection pool: `PublicAsyncSessionLocal` + PgBouncer capacity
**Priority P0. Depends on: P1-00.**

Section 4.5 in full: a second SQLAlchemy engine/sessionmaker bound to `dd_public`, `NullPool`, imported by nothing but `app/core/public_dependencies.py`, plus the PgBouncer stanza work (dedicated `[databases]` entry, raised `max_client_conn`, confirmed DO cluster `max_connections`).

**Acceptance criteria:** A grep confirms `PublicAsyncSessionLocal` is imported by exactly one module and uses `NullPool`. The product surface's sessions never resolve the keyhole policies — asserted by a test that sets `app.intake_token_hash` on a `dd_app` session and gets zero rows. App boots with a clear error, not a silent fallback to `dd_app`, if the public credential is missing. `pgbouncer.ini` shows a dedicated stanza for `dd_public` with its own `pool_size`, not the wildcard default; the DO cluster's `max_connections` capacity is confirmed and documented (or flagged as pending manual confirmation — see section 4.5).

### P1-03 — Keyhole RLS policies: `intake_token_lookup` + `intake_session_lookup`
**Priority P0. Depends on: P1-00, P1-01.**

Section 4.2 in full, plus the `dd_public` UPDATE policy on `deal_intake_link` with its `WITH CHECK` restricting writable status values.

**Acceptance criteria:** A `dd_public` session with `app.intake_token_hash` set to a live token's hash and no `app.org_id` set sees exactly that one row via policy A. A `dd_public` session with `app.intake_link_id` set to a live link's id sees exactly that row via policy B. Neither GUC satisfies the other's policy. An expired, revoked, or submitted link's row is invisible under both. A `dd_app` session with either GUC set sees nothing through the keyhole — the policies don't apply to it at all.

### P1-04 — `get_public_link_db` dependency (`app/core/public_dependencies.py`)
**Priority P0. Depends on: P1-03, P1-07.**

Section 4.3, `get_public_link_db` function.

**Acceptance criteria:** Tenant scope is derived from the database, never asserted by the caller. A malformed or unknown token yields 404 before any second query runs. Reading `clerk_org_id` off the link row succeeds with no `organisation`-table read in the call path at all. Both GUCs are set in the same statement, before any tenant-table query runs.

### P1-06 — `get_public_session_db` dependency + intake session JWT codec
**Priority P0. Depends on: P1-03, P1-07.**

Section 4.3, `get_public_session_db` function, plus the minimal `decode_intake_session_jwt`/`encode_intake_session_jwt` pair needed to make it testable (see the note at the end of section 4.3).

**Acceptance criteria:** A session JWT for org A's `link_id` cannot be used to read org B's data, even by hand-crafting `app.intake_link_id`, because policy B still has to resolve it. A Clerk-issued JWT fed to `decode_intake_session_jwt` is rejected (wrong audience), and vice versa for `decode_clerk_jwt`.

### P1-08 — Proof: `dd_public` cannot bypass RLS (`BYPASSRLS` / superuser / ownership)
**Priority P0. Depends on: P1-00.**

Section 4.6, item 1.

**Acceptance criteria:** `pg_roles` query for `dd_public` returns `rolbypassrls = false`, `rolsuper = false`. A `dd_public` session with `app.org_id` and `app.intake_link_id` both unset returns zero rows from `deal_intake_link` despite holding SELECT — proof RLS is actually binding, not merely configured.

### P1-09 — Exact `dd_public` grant-list assertion (fails the build on drift)
**Priority P0. Depends on: P1-00.**

Section 4.6, item 2. This supersedes any looser "standing check" language that might exist elsewhere in the ticket backlog (an earlier cross-cutting ticket, X-03, was rewritten to reference this ticket instead of duplicating it — if you see X-03 in the fuller backlog later, don't build a second version of this check).

**Acceptance criteria:** A migration that adds any grant to `dd_public` not present in the hardcoded expected set fails this test, by name, in CI — not a permission-denied surprise discovered in production. Removing a grant the flow depends on also fails it, the other direction of drift.

### P1-05 — Cross-tenant RLS + role-privilege negative test suite
**Priority P0. Depends on: P1-04, P1-06, P1-08, P1-09, P1-07. This is the last P1 ticket — it gates everything else.**

Against real Postgres: a valid link token for org A's deal returns zero rows when queried against org B's tables via `get_public_link_db`; a valid session JWT for org A's `link_id` returns zero rows against org B's tables via `get_public_session_db`; no GUC set returns zero rows under either policy; an expired/revoked/already-submitted link returns zero rows via both keyhole policies; the one-way status trigger rejects a second transition even as `doadmin`. Plus the role-boundary layer: `dd_public` raises `permission denied` on every table outside the grant matrix, enumerated table by table rather than sampled; a `dd_public` session scoped to deal A sees zero `data_source` rows for deal B in the *same* org; a `dd_app` session with the keyhole GUCs set sees nothing through them.

**Acceptance criteria:** Full suite green, covering both dependencies and both layers (policy scoping and role privilege) independently. This must be provable in isolation, by tests, before any UI is built on top of it.

**P1 is done when P1-05 is green.** Nothing in P2/P3 should start before that.

---

## 7. Status tracking file — create this now, in `docs/plans/external-deal-intake-link-status.md`

Use this template. Update it after every ticket, not just at the end. This is the file Vansh will read to know what happened — keep entries factual and specific (what you built, what you tested it against, what you found if anything deviated from this brief).

```markdown
# External Deal Intake Link — P1 implementation status

Started: <date>
Implementing session: local Claude Code CLI, Simpero_AI_Gov_Alpha
Source spec: docs/plans/external-deal-intake-link-implementation-brief.md

## Tickets

| Ticket | Status | Commit(s) | Tested against | Notes |
|---|---|---|---|---|
| P1-00 | not started / in progress / done | | | |
| P1-01 | | | | |
| P1-02 | | | | |
| P1-07 | | | | |
| P1-03 | | | | |
| P1-04 | | | | |
| P1-06 | | | | |
| P1-08 | | | | |
| P1-09 | | | | |
| P1-05 | | | | |

## Flagged (things that deviated from the brief, or decisions punted back to Vansh)

- <OPEN-1: which way you went on role creation (Alembic vs. init script), and why>
- <anything else>

## Open questions still unresolved (do not build)

- OPEN-1 (see section 4.7 of the brief) — resolved as: <...>
- OPEN-2 — Q17 malware scanning — out of scope for P1, untouched.
```

---

## 8. First prompt to paste into your local Claude Code CLI session

Once this file (and, ideally, `docs/plans/external-deal-intake-link-implementation-brief.md` copied into the repo) exist in your checkout of `Simpero_AI_Gov_Alpha`, start the session with something like:

> Read `docs/plans/external-deal-intake-link-implementation-brief.md` in full before doing anything else. It's a complete, decided spec for Phase P1 of the External Deal Intake Link feature — the `dd_public` Postgres role, three migrations, RLS keyhole policies, a second connection pool, and the negative test suite that proves it's safe. Implement the P1 tickets in section 6, in the order given, one at a time — after each ticket, verify its acceptance criteria against a real Postgres instance (this repo's dev Postgres via `docker-compose.dev.yml`, or CI's Postgres service container) before moving to the next. Update `docs/plans/external-deal-intake-link-status.md` after every ticket. Follow the operating rules in section 0 of the brief exactly, especially: don't build anything outside P1, don't resolve OPEN-1 or OPEN-2 silently, and flag anything that doesn't match what the brief describes instead of quietly adapting around it. Start with P1-00.

---

## 9. Where this brief's content came from

Everything in sections 1–6 is drawn directly from a plan document reviewed twice with Vansh and the PO (2026-08-24 initial review against all three repos' working trees; 2026-08-25 product review adding the `dd_public` role decision and three pre-close confirmations; a same-day follow-up audit adding upload-safety fixes). Nothing in those sections is new invention for this brief — it is a faithful, implementation-ordered restatement of decisions already made, so that implementing it here produces exactly what was planned, with zero gap between plan and build. The two explicitly open items (OPEN-1, OPEN-2) are open in the source plan too, not new gaps introduced by this handoff.
