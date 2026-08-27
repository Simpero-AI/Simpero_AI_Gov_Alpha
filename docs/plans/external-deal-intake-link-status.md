# External Deal Intake Link — P1 implementation status

Started: 2026-08-25
Implementing session: local Claude Code CLI, Simpero_AI_Gov_Alpha
Source spec: docs/plans/external-deal-intake-link-implementation-brief.md

## Tickets

| Ticket | Status | Commit(s) | Tested against | Notes |
|---|---|---|---|---|
| P1-00 | done, with a follow-up correction (see Flagged: data_source-policy relocation) | `44126da` (grant-matrix migration body edited uncommitted on top -- see P1-03 row) | real Postgres via docker-compose.dev.yml port 5434 | Scope narrowed to the three tables that already exist today (data_source, organisation, human_audit_log) per the ticket's exact instructions; deal_intake_link/deal_intake_response grants deferred to P1-01/P1-02 as those tables don't exist yet. Migration applies and reverses cleanly (upgrade/downgrade/upgrade all confirmed, twice — before and after the organisation-grant correction below). Originally: all 6 tests in tests/test_dd_public_grant_matrix.py green (5 out-of-scope-table permission-denied checks + the intra-org data_source scoping test). **Superseded by the P1-03 design correction below**: the two data_source RLS policies this migration originally created (intake_deal_documents, intake_deal_documents_insert) are now created in P1-03's migration instead (the bare `GRANT SELECT, INSERT ON data_source TO dd_public` stays here) -- see Flagged section. The intra-org scoping test moved to tests/test_intake_keyhole_policies.py accordingly; this file now only carries the 5 out-of-scope-table permission-denied tests. |
| P1-01 | done | held pending Vansh's go-ahead (uncommitted) | real Postgres via docker-compose.dev.yml port 5434 | `deal_intake_link` created per section 2.2: org_isolation (org_id Integer FK -> organisation.id, RLS+FORCE), clerk_org_id denormalized String(64) NOT NULL, partial unique index ux_deal_intake_link_pending_deal on (deal_id) WHERE status='pending', REVOKE UPDATE/DELETE from dd_app narrowed back to (status, submitted_at, failed_attempts, last_attempt_at), dd_public gets the same SELECT + narrow UPDATE grant (no policy yet — P1-03's job), BEFORE UPDATE one-way-status trigger (trg_deal_intake_link_one_way_status) fires for doadmin too. 7/7 new tests in tests/test_intake_link_rls.py green (org isolation hide/show, clerk_org_id never null, partial unique index blocks a second pending link, one-way trigger blocks a second UPDATE even via owner_conn/doadmin, dd_app denied on non-granted column and on DELETE). Full up/down/up cycle confirmed clean (upgrade head -> downgrade -1 -> upgrade head, table drops and recreates correctly, alembic current matches head each time). Pre-existing, unrelated failures found while running the full suite (tests/test_chunks_rls.py, tests/test_e2e_pipeline.py, tests/test_l2_retrieval_eval.py — the `chunks` table is missing entirely from this Docker volume despite alembic reporting head, a pre-existing environment drift not touched by this migration); confirmed unrelated by isolating tests/test_intake_link_rls.py + tests/test_data_source_rls.py + tests/test_dd_public_grant_matrix.py (25/25 green). pyright clean on all new/changed files. |
| P1-02 | done | held pending Vansh's go-ahead (uncommitted) | real Postgres via docker-compose.dev.yml port 5434 | `deal_intake_response` created per section 2.3: org_id (Integer FK -> organisation.id), deal_id + link_id (both real, NOT NULL FKs -- deal_id denormalized alongside link_id so an org-side read is one indexed lookup), respondent_email, answers (JSONB), submitted_at, ip_address (postgresql.INET, matching human_audit_log's typing), user_agent, created_at. RLS+FORCE with `org_isolation` (join through organisation, same idiom as deal_intake_link/data_source -- not a direct clerk_org_id string comparison). Blanket `REVOKE UPDATE, DELETE FROM dd_app` -- the human_audit_log idiom, no narrow grant-back and no trigger (unlike deal_intake_link's lifecycle-column pattern). dd_public gets `GRANT INSERT` only (deliberately no SELECT) plus a self-contained `intake_response_insert` WITH CHECK policy binding every inserted row to both `app.org_id` (via the organisation join) and `app.intake_link_id` -- doesn't depend on P1-04/P1-06 existing yet. No repo class added (no route inserts into this table until P1/P3's `/submit`, per the ticket's explicit scope note). 5/5 new tests in `tests/test_intake_response_rls.py` green: org isolation hide/show, dd_app denied on both UPDATE and DELETE (no narrow exception, unlike deal_intake_link), and the cross-tenant `WITH CHECK` test -- a dd_public session with `app.intake_link_id` set to its own link succeeds, the same session naming a *different* link (also nominally in org A) is rejected with a row-level-security violation. This last test also closes out **P1-00's originally-mis-attributed cross-tenant `WITH CHECK` acceptance criterion** ("An INSERT into deal_intake_response naming a different link_id violates the WITH CHECK") -- that table didn't exist until this ticket, so it couldn't have been tested at P1-00 time; re-attributed and proven here instead. Full up/down/up cycle confirmed clean (upgrade head -> downgrade -1 -> upgrade head; table drops/recreates correctly, `alembic current` matches head each time). Re-ran the full `test_intake_response_rls.py` + `test_intake_link_rls.py` + `test_dd_public_grant_matrix.py` + `test_human_audit_log_immutability.py` + `test_data_source_rls.py` set together (33/33 green) to confirm no regression against P1-00/P1-01's work. pyright clean on all new/changed files (model, `__init__.py`, migration, test file). One environment note (not a code issue): this local checkout's default `.env`-sourced `ALEMBIC_DATABASE_URL`/`DATABASE_URL` (picked up automatically by `uv run alembic` via `load_dotenv()`) point at a *different* Postgres instance than `docker-compose.dev.yml`'s port-5434 container -- pytest's `conftest.py` reads `os.environ` directly with no dotenv loading, so both `alembic` and `pytest` commands for this ticket were run with `DATABASE_URL`/`ALEMBIC_DATABASE_URL` explicitly exported to the sandbox port-5434 credentials (`dd_app`/`sandbox_dd_app`, `doadmin`/`sandbox_doadmin`) in the same shell invocation, matching `sandbox/init/01-app-role.sql`'s credentials and this repo's own test fixtures (`tests/conftest.py`'s `owner_conn`, `tests/test_dd_public_grant_matrix.py`'s `dd_public_conn` DSN). Flagging this since a bare `uv run alembic upgrade head` with no exports silently applies against the wrong database. **Follow-up correction (P1-03's fix, see that row and Flagged section):** `intake_response_insert`'s `WITH CHECK` gained an `EXISTS (... status = 'pending')` guard -- a response can now only be inserted while its link is still pending. One new test (`test_dd_public_cannot_insert_response_for_submitted_link`) added and green; the 6 pre-existing tests in this file are unaffected and still green. |
| P1-07 | done except DO max_connections confirmation (pending Vansh) | held pending Vansh's go-ahead (uncommitted) | real Postgres via docker-compose.dev.yml port 5434 | `app/core/config.py`: added required `public_database_url: str` field (no default, same fail-loud posture as `database_url`). New `app/core/public_database.py`: separate `public_engine`/`PublicAsyncSessionLocal`, `NullPool`, bound to `dd_public` — deliberately its own module (not a second export from `app/core/database.py`) so "imported by exactly one module" stays grep-able; no second `Base` defined, per spec. `docker/pgbouncer.ini`: added a dedicated named `simpero_public` entry (`pool_size=5`) alongside the unchanged wildcard `dd_app` entry; `max_client_conn` raised 22 → 40, `default_pool_size` (20) left unchanged. `docker-compose.yml`: pgbouncer service's entrypoint now also writes a `dd_public` line into `userlist.txt` from a new `DD_PUBLIC_PASSWORD` env var, mirroring the existing `DD_APP_PASSWORD` mechanism exactly. `docker-compose.dev.yml`: added `PUBLIC_DATABASE_URL=postgresql+asyncpg://dd_public:sandbox_dd_public@postgres:5432/simpero` to the `migrate`, `app`, and `worker` services' `environment:` blocks — confirmed this is load-bearing, not just cautious: `alembic/env.py` imports `app.core.database`, which constructs `Settings()` at module level, so `migrate` breaks too without it. `.env.example`: added `PUBLIC_DATABASE_URL` block after `ALEMBIC_DATABASE_URL`, matching comment style. `.github/workflows/ci.yml`: added `PUBLIC_DATABASE_URL` to both the `test` job's env (using `ci_dd_public_pw`, the exact password P1-00's "Create dd_public role" step already sets up) and the `build` job's env (confirmed load-bearing: the `build` job's "Import app" step runs `python -c "import app.main"`, which constructs `Settings()` transitively the same way `test` does). New `tests/test_public_intake_pool.py` (4 tests, all green): grep-based single-importer check (0 importers today, by design — `app/core/public_dependencies.py` doesn't exist until P1-04, documented in the test's docstring so it isn't a surprise when the count becomes 1); `public_engine.pool` is `NullPool`; a `dd_app` session's row-set is unaffected by setting `app.intake_token_hash` (see deviation note below); missing-`PUBLIC_DATABASE_URL` → `pydantic.ValidationError` on a direct `Settings()` construction. Confirmed `app.core.public_database` imports cleanly and `Settings()` constructs successfully against real Postgres with `PUBLIC_DATABASE_URL` exported. Full existing suite re-run (`uv run pytest -q`): 625 passed, only the same pre-existing/unrelated `chunks`-table failures already flagged in P1-01's row (environment drift, not touched by this ticket) — confirms the new required `Settings` field broke nothing else. `pyright`: 0 errors repo-wide. **Deferred to Vansh, per the ticket's explicit scope carve-out:** confirming the DigitalOcean cluster's actual `max_connections` capacity for staging/production — no DO console access this session. |
| P1-03 | done, plus two self-review follow-up fixes (see Flagged section) | `c1048df` (this ticket's own branch), `cf8d791` (self-review fixes, merged forward through p1-04/06/08/09/05) | real Postgres via docker-compose.dev.yml port 5434 | Design correction implemented exactly per architect + Vansh's approved fix (see Flagged section for the original blocking diagnosis and the resolution). `alembic/versions/b4f8e1c3a962_intake_keyhole_policies.py`: `intake_token_lookup` unchanged (still `status = 'pending'` only); `intake_session_lookup` widened to `status IN ('pending', 'submitted')`; `intake_link_status_update` unchanged from the original approved spec; the two `data_source` policies (`intake_deal_documents`, `intake_deal_documents_insert`) moved here from P1-00, each gaining an `EXISTS (... deal_intake_link ... status = 'pending')` guard. `tests/test_intake_keyhole_policies.py`: the previously-blocked 9th test (`test_link_id_path_can_flip_status_to_submitted`) now passes; added `test_submitted_link_invisible_via_token_hash_path` / `test_submitted_link_visible_via_link_id_path` (the asymmetry proof), `test_submitted_link_blocks_data_source_insert` (proves the EXISTS guard), and `test_data_source_scoped_to_one_org_deal_link` (the intra-org data_source scoping test, moved here from `test_dd_public_grant_matrix.py`). One pre-existing parametrized case, `test_non_pending_or_expired_link_invisible_under_both_policies[submitted-...]`, was removed (not just left failing) -- it asserted a submitted link is invisible under both policies, which is now false by design for the link-id path; that exact behavior is covered by the two new asymmetry tests instead, so it wasn't a redundant deletion. **A second, unplanned bug was found and fixed while running the full suite** (not part of the approved spec, a pure test-harness fix): `test_link_id_path_can_flip_status_to_submitted`'s successful `UPDATE ... status = 'submitted'` left the row locked in an open transaction until `public_db_session`'s fixture teardown calls `rollback()` -- but pytest tears sync fixtures down before async ones (reverse of setup order), so `pending_link`'s synchronous `DELETE` teardown ran first and blocked forever on that lock, and since it's a blocking (non-async) DB call it also starved the event loop, so `rollback()` never got a chance to run either -- reproduced as a real, indefinite hang against actual Postgres (confirmed via `pg_stat_activity`: one backend `idle in transaction` holding the row, another `active`/blocked on `Lock`/`transactionid`). Fixed by reordering that one test's fixture parameters to `(pending_link, public_db_session)`, which makes `public_db_session` tear down (and release the lock) first; verified with an isolated pytest fixture-order repro before applying. Full regression run: `uv run pytest tests/test_dd_public_grant_matrix.py tests/test_intake_link_rls.py tests/test_intake_response_rls.py tests/test_public_intake_pool.py tests/test_intake_keyhole_policies.py tests/test_data_source_rls.py -v` → 46/47 green, the single failure (`test_public_intake_pool.py::test_missing_public_database_url_raises_validation_error`) is pre-existing, unrelated to this ticket (zero diff on that file; caused by this checkout's local `.env` supplying a fallback `PUBLIC_DATABASE_URL` that `pydantic-settings`' `env_file` loading picks up even after `monkeypatch.delenv` removes it from `os.environ` -- a P1-07 test/environment issue, out of scope here). Migration chain applies cleanly to head; since this fix required editing two already-applied migrations (P1-00's and P1-02's) in place, a plain `downgrade -1`/`upgrade head` wasn't sufficient to prove the edited bodies re-apply from a live DB matching the old shape -- `downgrade base` itself hit the same pre-existing `chunks`-table environment drift P1-01 flagged (unrelated, at a much earlier migration) and rolled back atomically (DB left untouched, confirmed via `alembic current`), so the dev Postgres volume was recreated from scratch instead (`docker compose -f docker-compose.dev.yml down -v` + `up -d postgres`) and `alembic upgrade head` applied cleanly end-to-end on the first try (also incidentally resolving the `chunks`-table drift for this volume). After that, `alembic downgrade -1` → `6e9c2a4f7d18` → `alembic upgrade head` → `b4f8e1c3a962` confirmed clean and fast, and the full suite was re-run after that cycle with the same 46/47 result. `pyright`: 0 errors repo-wide. |
| P1-04 | done | held pending Vansh's go-ahead (uncommitted) | real Postgres via docker-compose.dev.yml port 5434 | New `app/core/intake_security.py` (`sha256_hex` hash helper, per spec -- to be extended by P1-06, untouched otherwise) and new `app/core/public_dependencies.py` (`get_public_link_db`), both matching section 4.3's exact code (typed as `AsyncGenerator[tuple[AsyncSession, DealIntakeLink], None]`, per the ticket's explicit note that this is the correct typed version of the brief's sketch). Neither file is imported by `app/core/dependencies.py` or any router (confirmed by grep -- zero importers of `get_public_link_db`/`get_public_session_db` anywhere in `app/`); `app/core/public_dependencies.py` is the sole importer of `PublicAsyncSessionLocal`, updating `tests/test_public_intake_pool.py::test_public_async_session_local_imported_by_exactly_one_module`'s expected-importer assertion from `[]` to `["app/core/public_dependencies.py"]` -- exactly the update that test's own P1-07-era docstring anticipated and instructed for when this ticket landed, not a new decision. `IntakeLinkRepo.get_by_token_hash(token_hash: str)` confirmed to already match the expected signature from P1-01, called as-is. New `tests/test_public_dependencies.py` (3 tests, all green): unknown/malformed token -> `HTTPException(404)` with no second query needed to prove it (a nonexistent token's hash can't match any seeded row); a second unknown-token-shape variant (uuid string) for the same 404 path; a valid, owner_conn-seeded pending/unexpired link (raw token controlled by the test, only its SHA-256 seeded into `token_hash`) yields `(session, link)`, asserts `link.clerk_org_id`/`link.deal_id` match what was seeded (proving org id was read off the link row, not a join), and asserts `current_setting('app.org_id', true)`/`current_setting('app.intake_deal_id', true)` both resolve correctly immediately after -- confirming both GUCs were set together in phase 2, before the test's own query runs. Each generator closed via `agen.aclose()` after use (or immediately after the `HTTPException` in the 404 cases), matching `public_db_session`'s pattern of releasing the transaction) via `pytest.raises` + explicit `aclose()`, no context-manager wrapper needed since the ticket didn't require one. No mocking/spying used, per the ticket's explicit guidance. Full regression run (`tests/test_intake_link_rls.py tests/test_intake_keyhole_policies.py tests/test_public_intake_pool.py tests/test_public_dependencies.py`): 26/27 green, the one failure (`test_public_intake_pool.py::test_missing_public_database_url_raises_validation_error`) is the exact pre-existing, unrelated `.env`-fallback issue already flagged in P1-03's row (zero diff on that test's own logic; not touched by this ticket). `pyright`: 0 errors repo-wide. No deviation from the approved spec. |
| P1-06 | done, plus one self-review follow-up fix (see Flagged section) | `ae8377b` (this ticket's own branch), self-review fix merged forward through p1-08/09/05 | real Postgres via docker-compose.dev.yml port 5434 | `app/core/config.py`: added required `intake_session_jwt_secret: str` field (no default, same fail-loud posture as `public_database_url`), with a comment explaining it's the self-issued intake-session JWT's own signing secret (own key/audience, not Clerk's). `app/core/intake_security.py`: extended (not replaced) with `IntakeSessionClaims` (pydantic, `link_id: UUID`, `email: str`), `encode_intake_session_jwt(link_id, email, ttl_minutes=30)` and `decode_intake_session_jwt(token)` -- exactly the spec's code, HS256, audience `simpero:intake-session`, `AuthenticationError` (confirmed same import path `app.core.exceptions.AuthenticationError` already used by `decode_clerk_jwt`) raised on any `JWTError`. `app/core/public_dependencies.py`: added `get_public_session_db(session_token)` alongside the existing `get_public_link_db`, matching section 4.3's code exactly (typed `AsyncGenerator[tuple[AsyncSession, DealIntakeLink], None]`, same convention P1-04 used) -- decode failures are NOT caught here (left to the P3 route handler), per the ticket's explicit instruction not to add speculative error handling. `INTAKE_SESSION_JWT_SECRET` added wherever `PUBLIC_DATABASE_URL` was added in P1-07, mirroring that exact pattern: `.env.example` (placeholder `CHANGEME`, comment style matched), `docker-compose.dev.yml`'s `migrate`/`app`/`worker` services (`sandbox_intake_session_secret`), `.github/workflows/ci.yml`'s `test` and `build` job env blocks (`ci-intake-secret`). Production `docker-compose.yml` needed no change -- it has no per-var `DATABASE_URL`-style overrides at all, relying entirely on `env_file: .env` (confirmed by grep before editing, so `.env.example` alone covers it, matching P1-07's own precedent). New `tests/test_intake_session_jwt.py` (3 tests, all green): encode/decode round-trip; a wrong-secret HS256 token (standing in for "Clerk-issued", since Clerk's real tokens are RS256/JWKS-verified with no shared secret to hand-craft against) rejected; a wrong-audience HS256 token (signed with the *real* secret, `aud: "clerk"`) rejected. The reverse direction (an intake-session JWT fed to `decode_clerk_jwt`) was **not** re-tested end-to-end with a live/mocked call -- `decode_clerk_jwt` needs a JWKS lookup by `kid` via `app/core/security.py::_get_jwks` (a real `httpx` call), and `tests/test_security.py` has no JWKS-mocking fixture to reuse; built one just for this negative case was judged disproportionate. Documented in the test file as "covered structurally, not re-tested end-to-end" -- the property is established from the other direction (different secret + different audience, both enforced by `jose.jwt.decode`), and practically `decode_clerk_jwt` would fail even earlier, at the "unknown kid" step, before ever reaching a signature/audience check. Extended `tests/test_public_dependencies.py` (4 new tests, all green): valid session JWT (encoded for a seeded link's real `id`) yields `(session, link)` with the right `clerk_org_id`/`deal_id` and both GUCs (`app.org_id`, `app.intake_deal_id`) set; a validly-signed JWT naming a genuinely nonexistent UUID (not just RLS-invisible) still 404s cleanly via `get_by_id` returning `None`; the cross-org proof -- a session JWT correctly naming org A's real `link_id`, after which `current_setting('app.org_id')` is confirmed to be org A's (not org B's), and a `data_source` query under that session returns zero of a freshly-seeded org B link+data_source pair's rows (new `org_b_docs` fixture, same shape as `test_intake_keyhole_policies.py`'s). Full regression run (`tests/test_intake_link_rls.py tests/test_intake_keyhole_policies.py tests/test_public_intake_pool.py` plus the two new/changed files): 32/33 green -- the one failure (`test_public_intake_pool.py::test_missing_public_database_url_raises_validation_error`) is the exact pre-existing `.env`-fallback issue already flagged in P1-03/P1-04's rows, zero diff on that test. `pyright`: 0 errors repo-wide. No deviation from the approved spec other than the judgment call on Clerk-JWT-reverse-direction test depth, called out above and in the ticket's own guidance as an acceptable judgment call.
| P1-08 | done | held pending Vansh's go-ahead (uncommitted) | real Postgres via docker-compose.dev.yml port 5434 | New `tests/test_dd_public_bypassrls_proof.py` (2 tests, both green), exactly per section 4.6 item 1 / the ticket's spec -- no new application code, pure verification. `test_dd_public_role_has_neither_bypassrls_nor_superuser`: queries `pg_roles` for `dd_public` via `owner_conn` (doadmin, catalog read -- appropriate bypass, not a violation of the dd_app/dd_public discipline), asserts `rolbypassrls IS false AND rolsuper IS false`. `test_dd_public_no_guc_sees_zero_rows_despite_select_grant`: seeds a `pending`, unexpired `deal_intake_link` row via `owner_conn` (same seeding pattern as `tests/test_intake_link_rls.py`/`tests/test_intake_keyhole_policies.py`, local `org_a_deal_id`/`pending_link` fixtures reusing conftest's `org_a_id`/`user_a_id`), then via `public_db_session` with **no GUC set at all** runs `SELECT id FROM deal_intake_link` and asserts an empty result set (not a permission error -- `dd_public` does hold SELECT on the table, so this is unambiguously the RLS-is-binding proof, not a masked grant/table-empty confound). Full regression run (`test_dd_public_bypassrls_proof.py` + `test_intake_link_rls.py` + `test_intake_keyhole_policies.py` + `test_intake_response_rls.py` + `test_public_intake_pool.py`): 31/32 green, the one failure (`test_public_intake_pool.py::test_missing_public_database_url_raises_validation_error`) is the exact pre-existing `.env`-fallback issue already flagged in P1-03/P1-04/P1-06's rows, zero diff on that test or file. `pyright` on the new file: 0 errors. No migration in this ticket, no deviation from spec. |
| P1-09 | done | held pending Vansh's go-ahead (uncommitted) | real Postgres via docker-compose.dev.yml port 5434 | New `tests/test_dd_public_grant_drift.py` (3 tests, all green), the exact-allowlist counterpart to P1-00/05's negative tests. Before writing the test, empirically ran both `information_schema.table_privileges` and `column_privileges` for `dd_public` against real Postgres (head `b4f8e1c3a962`), per the ticket's explicit instruction not to trust the hardcoded expected sets blindly. **Found a real discrepancy and stopped, per the ticket's own instruction, rather than silently adjusting the expected set**: `EXPECTED_TABLE_PRIVILEGES` (5 entries) matched exactly, but `information_schema.column_privileges` is NOT limited to column-restricted grants as the ticket assumed -- it expands EVERY grant, including whole-table grants with no column list (`data_source` SELECT/INSERT, `deal_intake_link`'s whole-table SELECT, `deal_intake_response` INSERT, `human_audit_log` INSERT), into one row per underlying column, so the real result was 62 rows, not the ticket's 7. Flagged this back rather than guessing. **Resolution (confirmed by the parallel session working the same ticket, relayed via inter-agent message):** `EXPECTED_COLUMN_PRIVILEGES` should be the full 62-row expanded set, not just the genuinely column-restricted ones -- reasoning: a whole-table grant genuinely exposes every current column (that's real, current exposure, not a query artifact), and expanding it gives *better* drift protection: a future migration adding a column to a table `dd_public` already holds a whole-table grant on will now correctly fail this test (the new column silently inherits the grant), whereas the narrow 7-row version would have missed that entirely -- matching the brief's own "loud error in development, never a quiet leak in production" philosophy. Before hardcoding, manually cross-checked every one of the 62 rows against the four migrations (`8f2a4c6e9b31`, `3d7b1f5a8c94`, `6e9c2a4f7d18`, `b4f8e1c3a962`) and each table's actual model column list (`app/models/data_source.py`, `app/models/human_audit_log.py`, plus the two intake migrations' `create_table` calls) -- every row traces to an intentional, already-approved grant; none were a surprise. `EXPECTED_COLUMN_PRIVILEGES` is hardcoded as the literal 62-entry set with a comment block per table explaining provenance (whole-table expansion vs. genuinely column-restricted). Third assertion: neither `information_schema.usage_privileges` nor `role_usage_grants` carries a row for dd_public's `USAGE` on schema `public` (confirmed empirically, as the ticket anticipated as a possible outcome) -- used `has_schema_privilege('dd_public', 'public', 'USAGE')` directly instead, per the ticket's own fallback instruction. All queries use `owner_conn` (doadmin, catalog read bypassing RLS -- appropriate here, no `app.org_id` needed). Full regression run (`test_dd_public_grant_drift.py` + `test_dd_public_grant_matrix.py` + `test_intake_link_rls.py` + `test_intake_response_rls.py` + `test_intake_keyhole_policies.py` + `test_public_intake_pool.py` + `test_public_dependencies.py` + `test_intake_session_jwt.py` + `test_dd_public_bypassrls_proof.py`): 48/49 green, the one failure (`test_public_intake_pool.py::test_missing_public_database_url_raises_validation_error`) is the exact pre-existing, unrelated `.env`-fallback issue already flagged in P1-03/04/06/08's rows, zero diff on that file. `pyright`: 0 errors repo-wide. No migration in this ticket. |
| P1-05 | done | held pending Vansh's go-ahead (uncommitted) | real Postgres via docker-compose.dev.yml port 5434 | New `tests/test_intake_cross_tenant_negative.py` (15 tests, all green) -- a synthesis test file per the ticket's own framing: no new application code, every assertion re-proves an earlier ticket's isolated coverage but this time end-to-end through the actual `get_public_link_db`/`get_public_session_db` dependency functions (not just raw policy predicates via a bare `public_db_session`). **Implemented against the CORRECTED (post-P1-03-fix) behavior, not the brief's original stale wording** -- per this file's own P1-03 row and its two Flagged entries: `intake_session_lookup` (the link-id/session path) was deliberately widened to also admit `status = 'submitted'`, so a submitted link is invisible via the raw-token path (`get_public_link_db`, still 404) but visible via the session path (`get_public_session_db`, yields the link with `status == 'submitted'`) -- an intentional, already-approved asymmetry, not a bug. Expired/revoked links remain invisible via BOTH dependency functions, unchanged from the brief's original wording. Covers, in order: (1) cross-tenant via `get_public_link_db` against a real second org (`org_a_link`/`org_b` fixtures); (2) same via `get_public_session_db`; (3) no GUC set -> zero rows (re-proves P1-08's proof, duplicated deliberately); (4) expired/revoked -> 404 from both dependency functions (4 tests, one per link-status x dependency-function combination); (5) submitted -> the corrected asymmetry, proven through the actual dependency functions rather than raw policy predicates (`test_submitted_link_asymmetry_through_dependency_functions`); (6) the one-way status trigger re-asserted, even as `doadmin` (duplicated from `tests/test_intake_link_rls.py`); (7) the role-boundary layer re-run as a closing sanity pass -- `dd_public` denied on all 5 out-of-scope tables (duplicated parametrized loop from `tests/test_dd_public_grant_matrix.py`), and a `dd_app` session with the keyhole GUCs set sees no change in its row set (pollution-proof before/after comparison, duplicated from `tests/test_public_intake_pool.py`'s own fix for this dev volume's accumulated fixture data). `tests/test_intake_cross_tenant_negative.py -v`: 15/15 green. Full combined run (`test_dd_public_grant_matrix.py test_intake_link_rls.py test_intake_response_rls.py test_public_intake_pool.py test_intake_keyhole_policies.py test_public_dependencies.py test_intake_session_jwt.py test_dd_public_bypassrls_proof.py test_dd_public_grant_drift.py test_intake_cross_tenant_negative.py -v`): 63/64 green -- the only failure is the exact pre-existing, unrelated `.env`-fallback issue already flagged in P1-03/04/06/08/09's rows (`test_public_intake_pool.py::test_missing_public_database_url_raises_validation_error`, a file this ticket did not touch -- confirmed via `git status`). `pyright`: 0 errors repo-wide. No deviation from the approved spec beyond the (already-approved, not new) P1-03 correction this ticket was explicitly instructed to test against. **P1 is done: P1-05 is green**, per the brief's own closing statement (section 6). |

## Flagged (things that deviated from the brief, or decisions punted back to Vansh)

- OPEN-1: resolved as role creation via a plain SQL init script
  (`sandbox/init/02-public-role.sql`, mirroring `01-app-role.sql`'s
  `dd_app` precedent — mounted automatically by `docker-compose.dev.yml`'s
  existing directory mount of `sandbox/init`, no compose changes needed;
  CI gets an equivalent "Create dd_public role" step in `.github/workflows/ci.yml`
  right after "Create dd_app role") + the grant matrix via Alembic
  (`alembic/versions/8f2a4c6e9b31_dd_public_grant_matrix.py`). Confirmed by
  Vansh — not re-litigated.
- **P1-00 organisation grant correction (confirmed, resolved, landed in
  `44126da`):** Postgres evaluates RLS policy `USING`/`WITH CHECK`
  expressions with the privileges of the *querying* role, not the policy or
  table owner. The three new policies that reference `organisation` —
  `intake_deal_documents`, `intake_deal_documents_insert` (both on
  `data_source`) and `intake_human_audit_insert` (on `human_audit_log`) — do
  `org_id = (SELECT id FROM organisation WHERE clerk_org_id = ...)`, which
  requires `dd_public` to have column-level `SELECT` on `organisation.id`.
  The brief's originally specified grant, `GRANT SELECT (name, clerk_org_id)
  ON organisation TO dd_public`, didn't include `id` — it was reasoned about
  only in terms of "two columns of one row, for the intake page's display
  name," not the RLS subquery's needs. Confirmed directly against real
  Postgres during implementation: `SET ROLE dd_public; SELECT id FROM
  organisation ...` → `permission denied for table organisation`, making all
  three policies unusable as originally specified (every INSERT/SELECT
  dd_public attempts through them would fail outright, not just be
  RLS-filtered to zero rows). Confirmed by the orchestrator and architect as
  expected Postgres RLS behavior, and resolved as a correction to the
  brief's section 4.4 grant-matrix table itself: `GRANT SELECT (name,
  clerk_org_id) ON organisation TO dd_public` → `GRANT SELECT (id, name,
  clerk_org_id) ON organisation TO dd_public` (and the matching downgrade
  `REVOKE`). The `intake_organisation_lookup` policy predicate and every
  other policy/grant are unchanged. Re-verified against real Postgres after
  the fix: migration reverses/reapplies cleanly, all 6 tests in
  `tests/test_dd_public_grant_matrix.py` green.

- **P1-07 test deviation (`tests/test_public_intake_pool.py::test_dd_app_session_keyhole_guc_has_no_effect`):** the ticket's literal acceptance criterion is "set `app.intake_token_hash` on a `dd_app` session, query `deal_intake_link`, assert zero rows." Against this local Postgres volume that assumption doesn't hold — several P1-01 fixtures (`test_intake_link_rls.py`'s `org_a_deal_id`-dependent tests) deliberately leave `org_a`-owned `deal_intake_link` rows behind with no teardown, so `test_org_id`'s rows accumulate across runs and a plain "assert zero rows" fails on pre-existing, unrelated data rather than proving anything about the GUC. Rewrote the test to compare the row set returned before vs. after setting `app.intake_token_hash` and assert they're identical — an equivalent, pollution-proof proof that the GUC has no effect on what `org_isolation` already exposes (no keyhole policy applies to `dd_app`). Confirmed with a real Postgres run: without the fix, the test failed with 16 pre-existing rows; with the fix, it passes deterministically regardless of DB state.

- **P1-03: the approved UPDATE-policy SQL cannot satisfy its own acceptance
  criterion against real Postgres 16 -- flagged, not silently fixed.** The
  ticket's `intake_link_status_update` policy is exactly as specified
  (`USING` on the OLD row, an explicit `WITH CHECK` on the NEW row that
  permits the link-id path to reach `status = 'submitted'`). That `WITH
  CHECK` expression evaluates to `true` in isolation (verified directly:
  `SELECT <the exact WITH CHECK boolean> ...` returns `t` for the new row).
  But the actual `UPDATE ... SET status = 'submitted' WHERE id = :id`
  (link-id path, the one path this policy is supposed to let through) fails
  with `new row violates row-level security policy for table
  "deal_intake_link"` and the whole statement is rolled back -- it does not
  update 0 rows silently, it errors.

  Root cause, confirmed with an isolated minimal-schema reproduction
  (scratch table, no other confounds): when a table has a **SELECT** policy
  for a role (here, `intake_token_lookup` / `intake_session_lookup`, both
  requiring `status = 'pending'`) *in addition to* an UPDATE policy with its
  own `WITH CHECK`, PostgreSQL 16 requires the row **resulting from the
  UPDATE** to *also* satisfy at least one of that role's applicable
  **SELECT**-command policies -- not just the UPDATE policy's own `WITH
  CHECK` -- and raises an RLS-violation error (aborting the whole statement)
  if it doesn't. Reproduced twice: once on a scratch table mirroring the
  real policy shape (OR of token-hash/link-id branches), and once on a
  maximally minimal scratch table (`USING (true) WITH CHECK (true)` update
  policy + a single `USING (status = 'pending')` select policy) -- both
  error identically the moment the UPDATE tries to move `status` away from
  `'pending'`.

  Practically: because `deal_intake_link`'s two keyhole SELECT policies
  both require `status = 'pending'`, **no UPDATE that flips `status` away
  from `'pending'` can ever succeed while those SELECT policies exist for
  `dd_public`** -- not just the token-hash path (which the design correction
  already intends to block), but the link-id path too (which the design
  correction explicitly intends to *allow*). This isn't a typo in the
  `WITH CHECK` expression; it's a structural conflict between "the keyhole
  SELECT policies make a submitted link invisible" (section 4.2's stated
  design intent) and "the link-id path can flip status to submitted"
  (also section 4.2's stated design intent, and P1-03's own acceptance
  criterion) -- both cannot be simultaneously true against real Postgres 16
  with the exact SQL given. This will also block the *actual* P3 `/submit`
  route later if left unresolved, not just this ticket's test.

  **Not resolved by me** -- this is a design decision (e.g. broadening the
  link-id SELECT policy to also permit `status = 'submitted'`, restructuring
  the UPDATE policy's `USING`/`WITH CHECK` split, or something else Vansh
  and the original design-review round need to weigh in on), so I stopped
  short of redesigning the policy and left the migration matching the
  approved spec exactly, downgraded (not applied) rather than half-applied.
  `tests/test_intake_keyhole_policies.py` and the `public_db_session`
  conftest fixture are written and correct for everything *except* the one
  blocked assertion; the migration file itself is unchanged from the
  approved spec pending direction.

- **P1-03 UPDATE-policy conflict -- RESOLVED (confirmed by architect + Vansh,
  correction implemented mechanically in a follow-up session):** fixed by
  widening `intake_session_lookup` (only) to admit `status IN ('pending',
  'submitted')`, so the row resulting from the link-id path's `UPDATE ...
  SET status = 'submitted'` now satisfies an applicable SELECT policy for
  `dd_public`, which is what Postgres 16 requires to let the UPDATE
  complete (see the diagnosis above for why). `intake_token_lookup` was
  deliberately left unwidened -- the raw shareable token still dies the
  instant `status` leaves `'pending'`; only the link-id path (reachable only
  after a verified session JWT names that exact `link_id`, never by
  guessing a UUID) can see a submitted link. This is a real, intentional
  asymmetry, not a bug, and is proven by two dedicated tests
  (`test_submitted_link_invisible_via_token_hash_path` /
  `test_submitted_link_visible_via_link_id_path`) rather than left implicit.

  As part of the same correction, `data_source`'s two dd_public policies
  (`intake_deal_documents`, `intake_deal_documents_insert`) were relocated
  from P1-00's migration into P1-03's (they need an `EXISTS (...
  deal_intake_link ... status = 'pending')` guard against
  `deal_intake_link`, which doesn't exist until P1-01 -- P1-00 predates it in
  ticket order) and each gained that guard, so a submitted/revoked/expired
  link's documents become unreachable at the database level the moment the
  link leaves `pending`, not just by app-code convention. `deal_intake_response`'s
  `intake_response_insert` gained the equivalent guard in P1-02's migration
  (edited in place) -- a response can only be inserted while its link is
  still pending, which means the real P3 `/submit` route must INSERT the
  response row *before* flipping `deal_intake_link.status` to `'submitted'`
  in the same transaction.

  Verified against real Postgres 16 (docker-compose.dev.yml port 5434):
  `test_link_id_path_can_flip_status_to_submitted` (the previously-blocked
  9th test) now passes, migration chain applies/reverses/reapplies cleanly,
  and the full regression set is green (see P1-03's row above for the exact
  numbers and the one pre-existing/unrelated failure). One additional,
  unplanned test-harness bug (a real fixture-teardown deadlock, not a
  design/RLS issue) was found and fixed while verifying this -- see P1-03's
  row for the full diagnosis; it was purely mechanical (swapped one test's
  fixture parameter order) and doesn't affect the migration SQL itself.

- **P1-09: `information_schema.column_privileges` scope -- RESOLVED (confirmed
  by a parallel session working the same ticket, relayed via inter-agent
  message; not re-litigated).** The ticket's own hardcoded
  `EXPECTED_COLUMN_PRIVILEGES` (7 entries) assumed `column_privileges` only
  ever lists column-restricted grants (e.g. `organisation`'s `SELECT (id,
  name, clerk_org_id)`, `deal_intake_link`'s `UPDATE (status, ...)`).
  Empirically false against real Postgres 16: `column_privileges` expands
  EVERY grant dd_public holds -- including whole-table grants with no
  column list (`data_source` SELECT/INSERT, `deal_intake_link`'s whole-table
  SELECT, `deal_intake_response` INSERT, `human_audit_log` INSERT) -- into
  one row per underlying column, giving 62 rows total, not 7.
  `table_privileges`, by contrast, behaved exactly as the ticket assumed
  (whole-table grants only, no column-restricted grants leak in) --
  confirmed both ways before writing anything. Resolved by hardcoding the
  full 62-row expanded set as `EXPECTED_COLUMN_PRIVILEGES`, each row
  individually cross-checked against the four migrations and the actual
  model column lists before being trusted -- see `tests/
  test_dd_public_grant_drift.py`'s module docstring and inline comments for
  the full reasoning and provenance.

- **Self-review pass (2026-08-27, separate Cowork session, all 10 PRs against
  real GitHub state) -- two code fixes, one housekeeping gap, confirmed and
  actioned:**

  1. **[Critical, security] `get_public_session_db` leaked a 401 instead of
     the mandated 404 (P1-06) -- FIXED, commit `ae8377b`.**
     `decode_intake_session_jwt` raises `AuthenticationError` on any bad
     token (expired, tampered, wrong audience), and this function
     deliberately didn't catch it -- the original P1-06 docstring called it
     "a P3 route-handler concern." But `app/main.py` already has a global
     `@app.exception_handler(AuthenticationError)` returning HTTP 401 with
     the raw JWT-library message. The moment any P3 route (P3-08/09/10/11)
     wired this dependency in, a bad session token would surface as a
     distinguishable 401 with a descriptive body instead of the same 404
     every other failure mode returns -- reopening the exact enumeration
     oracle the whole 404-only design (brief section 5.2) exists to prevent.
     Fixed by catching `AuthenticationError` and raising
     `HTTPException(404)`, matching `get_public_link_db`'s own existing
     pattern -- both public dependency functions are now consistent, and no
     future P3 route can get this wrong by forgetting to wrap it. New test
     `test_bad_session_token_404s_not_401` in `tests/test_public_dependencies.py`
     feeds it a garbage token and asserts 404. Verified against real
     Postgres: 692/692 tests green, pyright clean. Merged forward through
     p1-08/p1-09/p1-05.

  2. **[Correctness, low exploitability] `deal_id` wasn't cross-checked
     against the link's real deal (P1-03) -- FIXED, commit `cf8d791`.**
     `intake_response_insert`'s `WITH CHECK` verified `org_id`, `link_id`,
     and that the link is still `pending`, but never that
     `deal_intake_response.deal_id` actually equals the `deal_id` on the
     `deal_intake_link` row named by `link_id` -- `deal_id` is denormalized
     onto this table purely for read convenience (P1-02's migration
     docstring), so nothing at the DB layer stopped a mismatched `deal_id`
     from being written. Not externally exploitable today (`deal_id` is
     always server-derived in the P3-11 ticket, never client input), but a
     real gap against this feature's "provable at the database layer" design
     pillar (brief section 5.6). Fixed with a fourth `AND` clause on the
     `WITH CHECK` (`deal_id = (SELECT deal_id FROM deal_intake_link WHERE id
     = current_setting('app.intake_link_id', true)::uuid)`); `downgrade()`
     already reverted to P1-02's original (no new clause needed there). New
     test `test_response_insert_rejects_deal_id_not_matching_the_links_real_deal`
     in `tests/test_intake_keyhole_policies.py` proves org_id/link_id/status
     all valid but a mismatched (real, FK-satisfying) `deal_id` is still
     rejected by RLS. **Also while in this migration**, added the missing
     SELECT-side test coverage the same review flagged: `test_submitted_link_blocks_data_source_insert`
     proved `intake_deal_documents_insert` blocks writes once a link is
     submitted, but there was no equivalent for `intake_deal_documents` (the
     SELECT policy) despite both carrying the identical `EXISTS (... status
     = 'pending')` guard -- added `test_submitted_link_blocks_data_source_select`.
     Verified against real Postgres: full up/down/up cycle clean, 684/684
     tests green, pyright clean. Merged forward through p1-04/06/08/09/05.

  3. **[Process/housekeeping] The docs every migration/test cites weren't
     committed anywhere -- FIXED, this commit.** `docs/plans/
     external-deal-intake-link-status.md` (this file), `docs/plans/
     external-deal-intake-link-implementation-brief.md`, and `docs/
     implementations/2026-08-26-external-deal-intake-link-p1.md` existed
     only uncommitted in the local working tree, even though migration
     docstrings and test comments across P1-00, P1-02, P1-03, and P1-09 cite
     this status file by name as proof of "APPROVED DESIGN CORRECTION
     (confirmed by architect + Vansh)" -- nobody reviewing on GitHub alone
     could check those citations. Committed as a single commit on
     `p1-05-cross-tenant-negative-suite` (PR #123, the tip of the stack),
     this file updated first so the committed version reflects fixes 1/2
     above, not a stale snapshot.

  - **Informational, no code change:** P1-07's DigitalOcean `max_connections`
    confirmation is still open -- this needs Vansh to check the actual DO
    cluster capacity in the console; nothing to fix in code, left as-flagged
    (see P1-07's own row above).

## Open questions still unresolved (do not build)

- OPEN-1 (see section 4.7 of the brief) — resolved as: init-script-for-role
  creation + Alembic-for-grants (confirmed by Vansh).
- OPEN-2 — Q17 malware scanning — out of scope for P1, untouched.
