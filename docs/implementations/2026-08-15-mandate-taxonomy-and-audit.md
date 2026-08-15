# Mandate Taxonomy, Mandate Save, and Mandate-Save Audit Trail — Implementation Summary

**Status:** Implemented, type-checked, and manually verified end-to-end against a local Docker dev stack (real Postgres, real migrations). **Not committed, not pushed, not applied to any shared/staging database.** All three pending migrations (`d507a017730a`, `f3a8c9d2e1b7`, `a1c3e7f2b4d9`) exist only as local files plus a one-time run against a throwaway `docker-compose.dev.yml` Postgres volume used for verification.
**Plans followed:** `docs/plans/mandate-options-widen-option-column.md`, `docs/plans/mandate-options-sub-options.md`, `docs/plans/mandate-save-audit-log.md`, `docs/plans/mandate-save-audit-detail.md` — all four are cross-repo addenda written by a `Simpero_AI_Gov_Web` frontend session, implemented here as-specified. The base mandate CRUD surface (models/repos/schemas/both routers) these addenda build on was **not** plan-doc-driven — built directly from conversational requests earlier in this same session, with several design decisions (admin-vs-product write split, cardinality, RLS shape) resolved via clarifying questions before writing code.
**Session:** 2026-08-14 through 2026-08-15.

---

## What this feature is

A platform-admin-managed mandate taxonomy (categories → options → nested sub-options), plus a per-org "Mandate Builder" save surface on the product portal, plus an audit trail of every save with a computed diff of what changed.

Three tables, two portals:

- **`mandate_categories`** / **`mandate_options`** — global reference data (no `org_id`, no RLS), shared across every tenant. Full CRUD restricted to platform admins (`app/api/admin/mandates.py`); read-only on the product portal (`app/api/mandates.py`). Options can nest arbitrarily deep via a self-referential `parent_option_id` (first concrete use: Geographies → Canada → provinces).
- **`mandates`** — one row per org (unique `org_id`), RLS-protected like every other tenant table. Set via `PUT /mandate` on the product portal; any org member can save it. `user_id` records who last saved it, informationally.
- **`human_audit_log`** — every mandate save now writes a `mandate_saved` row with `actor_id`/`actor_email` and a computed diff of what changed, readable back through the existing `GET /logs/recent-activity` endpoint (which also gained `actorEmail`/`payload` fields, generically, for every event type).

---

## What was built

### Database (one migration, revised four times in place)

Everything folded into **`alembic/versions/a1c3e7f2b4d9_mandates.py`** rather than stacked across separate migrations — verified via `alembic heads` vs `alembic current` before every revision that the migration had not shipped anywhere (local head stayed ahead of the applied revision on every check), so each addendum's own guidance ("fold into the unshipped migration, don't stack") applied cleanly every time.

Final shape:
- `mandate_categories(id, category)` — `category` `String(150)`, `UNIQUE`.
- `mandate_options(id, category_id → mandate_categories ON DELETE CASCADE, parent_option_id → mandate_options ON DELETE CASCADE NULLABLE, option)` — `option` `String(255)` (widened from an initial `String(50)` per the first addendum, before any full-sentence mandate criteria could have overflowed it). Two **partial** unique indexes replace one plain unique index: `(category_id, option) WHERE parent_option_id IS NULL` (top-level rows, unique per category) and `(parent_option_id, option) WHERE parent_option_id IS NOT NULL` (child rows, unique per parent) — so two different parents (e.g. two different top-level options) can each have a child option with the same name without colliding.
- `mandates(id, org_id UNIQUE → organisation, user_id → users, mandate JSONB, created_at, updated_at)` — RLS enabled, `org_isolation` policy identical in shape to `deals`/`investment_profiles`/`claims`.

**Deliberately no ORM `relationship()`** on `parent_option_id` — `MandateOptionsRepo.delete` calls `session.delete()` directly, and a configured self-referential relationship would make SQLAlchemy try to `NULL` out children's `parent_option_id` on delete instead of letting Postgres cascade, silently promoting sub-options to top-level rows. The `ON DELETE CASCADE` at the DB level is what actually deletes a subtree.

Two other migrations from earlier in this session, unrelated to mandates but sequenced before it in the same still-unshipped chain: `d507a017730a` (`deals.user_id` FK, nullable, informational creator metadata) and `f3a8c9d2e1b7` (`claims.deal_id` made `NOT NULL` after confirming zero orphaned rows and deleting nine pre-existing NULL test rows).

### Python modules

| File | Purpose |
|---|---|
| `app/models/mandate.py` | `MandateCategory`, `MandateOptions`, `Mandate` ORM models |
| `app/repo/MandateCategoryRepo.py` | `create`, `get_by_id`, `list`, `update`, `delete` |
| `app/repo/MandateOptionsRepo.py` | `create`, `get_by_id`, `list_all`, `list_by_category`, `update`, `delete` |
| `app/repo/MandateRepo.py` | `create`, `get_by_id`, `get_for_org`, `upsert` (create-or-replace on unique `org_id`) |
| `app/schemas/mandate.py` | Product-facing schemas — recursive `MandateOptionResponse` (`sub_options: list[...] = []`) |
| `app/schemas/admin/mandate.py` | Admin-facing schemas — same recursion, plus `category_id`/`parent_option_id` |
| `app/api/mandates.py` | Product router — see API surface below |
| `app/api/admin/mandates.py` | Admin router — see API surface below |

**Modified, not new:** `app/models/__init__.py` (registered the three new models for Alembic autogenerate), `app/api/admin/__init__.py` / `app/main.py` (router registration), `app/schemas/logs.py` / `app/api/logs.py` (`actorEmail`/`payload` on `ActivityRowResponse`).

Repos are shared between both routers (`MandateCategoryRepo`/`MandateOptionsRepo` used from both `app/api/mandates.py` and `app/api/admin/mandates.py`) — same precedent as `HumanAuditRepo` being used from both admin and product routers already. Schemas are **not** shared, per CLAUDE.md's admin/product separation rule — `app/schemas/mandate.py` and `app/schemas/admin/mandate.py` each define their own `MandateOptionResponse` even though the shapes mostly overlap.

### API surface (final)

**Product portal** (`app/api/mandates.py`, no router-level prefix):
- `GET /mandate-categories` — every category with its options nested recursively (`sub_options` present at every depth, `[]` when empty). Read-only.
- `GET /mandate` — the org's own mandate, `null` (never 404) if unset.
- `PUT /mandate` — create-or-replace the org's mandate. Computes a diff against the previous value and writes it, plus a `mandate_saved` audit row, in the same transaction as the upsert.

**Admin portal** (`app/api/admin/mandates.py`, prefix `/admin/mandates`, every route gated by `require_platform_admin` + `get_admin_db`):
- `GET /categories` — same nested shape as the product read, plus `parentOptionId`/`categoryId` on each node.
- `POST /categories`, `PATCH /categories/{id}`, `DELETE /categories/{id}` (cascades to every option/sub-option under it).
- `POST /categories/{category_id}/options` — create a top-level option.
- `POST /options/{parent_option_id}/suboptions` — create a child option. A route, not an optional `parentOptionId` body field on the endpoint above: `category_id` is derived from the parent row, so "parent belongs to a different category than the path" can't be expressed — no validation branch to write or forget.
- `PATCH /options/{id}`, `DELETE /options/{id}` — work on any option regardless of depth; delete cascades the whole subtree.

Every create/update on categories and options catches `IntegrityError` from the unique-constraint checks and returns `409 Conflict` instead of a raw `500` (added during this session, not part of any addendum doc — found while explaining a gap the addenda themselves called out as pre-existing and out of scope, then fixed on request).

### Mandate-save audit trail

`PUT /mandate` now, in order: fetches the org's previous mandate, computes an entry-level diff against the incoming body (`_diff_mandate`, ~65 lines, handles both mandate entry shapes — category+options and the Check-Size-Range min/max shape — plus nested sub-option adds/removes), upserts the new mandate, then writes one `human_audit_log` row (`event_type="mandate_saved"`, `actor_id`/`actor_email` from the caller, `payload` = the diff). Unchanged categories are omitted from the diff entirely; an empty diff list means "nothing actually changed."

`GET /logs/recent-activity` (`app/schemas/logs.py` / `app/api/logs.py`) gained `actorEmail: str | None` and `payload: Any | None` on `ActivityRowResponse`, read straight off the two `human_audit_log` columns that already existed — generic across every `event_type` in the system, not mandate-specific, so this is additive for every existing writer (`deal_created`, `auth_login`, etc.) with no other writer needing changes.

---

## Deviations from the addenda, flagged during implementation

- **`mandate-options-sub-options.md`'s "Out of scope" section** claimed the duplicate-name-500 gap was still unaddressed and suggested fixing it "if ever picked up." It had already been fixed earlier in this session (all four `mandate_options`/`mandate_categories` write paths, plus the new sub-option-create endpoint added by that same doc) — flagged to Vansh rather than silently reconciling the doc's stale assumption.
- **`mandate-save-audit-detail.md`'s exact snippet** — `_diff_mandate(previous.mandate if previous else [], body.mandate)` — doesn't type-check: `Mandate.mandate` is `Mapped[list | None]` (nullable JSONB), so `previous.mandate` can be `None` even when `previous` itself exists, and `_diff_mandate`'s `old: list[Any]` parameter isn't `Optional`. Fixed by mirroring the same `or []` guard `get_mandate` already uses elsewhere in the same file: `(previous.mandate or []) if previous else []`. Confirmed via `uv run pyright` before and after.

---

## RLS / tenant-isolation shape (confirmed, not just asserted)

- **`mandates`**: RLS enabled, `org_isolation` policy scoped on `org_id`. One org can never read or overwrite another's mandate — enforced at the database, not the application layer.
- **`mandate_categories` / `mandate_options`**: deliberately **no** RLS — no `org_id` column exists on either table, by design (shared taxonomy, identical across every tenant). Write access is restricted at the application layer instead, via `require_platform_admin` on every admin-router mutation; there's no tenant dimension for RLS to scope against on these two tables.

---

## Verification performed

- `uv run pyright`: 0 errors, project-wide, after every round of changes (four addenda plus the base build).
- `uv run ruff format`: clean.
- Full `docker-compose.dev.yml` stack (fresh Postgres, migration container) rebuilt from scratch after resolving an unrelated stale-Docker-network error; the `migrate` service ran and exited `0`, applying all three pending migrations — confirms the folded-together `a1c3e7f2b4d9` migration (widened column, `parent_option_id`, both partial indexes, `mandates` table + RLS) actually runs cleanly end-to-end, not just parses.
- FastAPI app builds its OpenAPI schema with all mandate routes correctly registered (7 routes across both routers, including the sub-option endpoint).
- Recursive schema smoke test: confirmed `MandateOptionResponse` serializes nested `sub_options` correctly with camelCase keys (`subOptions`, `parentOptionId`) at arbitrary depth via `model_rebuild()`.
- End-to-end audit-write smoke test against the local dev Postgres: seeded a throwaway org/user, replicated `upsert_mandate`'s exact call sequence, confirmed the resulting `human_audit_log` row matched expected shape (`event_type='mandate_saved'`, correct `actor_id`/`actor_email`/`org_id`, `deal_id`/`session_id`/`payload` all `None` — base addendum, before the diff).
- Ran `mandate-save-audit-detail.md`'s own verification scenario against the local dev Postgres: two sequential saves (add Series A to Investment Stage; then remove it and add Canada → British Columbia to Geographies). Resulting `payload` diffs matched the doc's expected output exactly — first row showed only the Investment Stage addition, second showed the Investment Stage removal plus the Geographies/Canada addition with `subOptionsAdded`, with no mention of untouched categories in either.
- All test/scratch rows (organisations, users, audit entries) cleaned up after each verification run via the doadmin connection (audit rows can't be deleted by `dd_app` — `UPDATE`/`DELETE` are revoked at the database level, by design).

---

## What's still needed

- **Nothing has been applied to the shared DO cluster.** `alembic upgrade head` has only ever run against the disposable `docker-compose.dev.yml` Postgres volume for verification. Running it for real against staging/production is a separate, explicit step — not taken in this session.
- **Nothing has been committed.** All files listed above are unstaged/untracked as of this writing.
- **No seed data** for the taxonomy (explicitly out of scope per the addenda) — `mandate_categories`/`mandate_options` start empty; a platform admin populates them by hand through the new admin CRUD endpoints.
- **No stable cross-environment join key** for taxonomy rows (also explicitly out of scope) — IDs are server-generated UUIDs, so "the Sector category" only exists as a name at runtime, not a deterministic ID any other system can reference across environments.
- Optional follow-up noted in the audit-detail addendum but not requested: no mandate version-history table exists — the audit log's diff payloads let a reader see *what changed per save*, but reconstructing "the mandate as of time T" from a chain of diffs is not built.
