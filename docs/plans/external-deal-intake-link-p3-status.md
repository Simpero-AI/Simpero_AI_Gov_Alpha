# External Deal Intake Link — P3 implementation status

Started: 2026-08-27
Implementing sessions: Vansh (local Claude Code CLI), Suraj (local Claude Code CLI)
Source spec: docs/plans/external-deal-intake-link-phase-3.md
Base branch decision (section 0.2): **Option A, with a correction to section 0.1** — P1 (#114-123) is
now fully merged to `staging` (confirmed via `git diff origin/staging staging --stat` showing no
diff, and a single Alembic head `b4f8e1c3a962` before this branch's own migration). The brief's
section 0.1 table is stale on this point. `p3-base` was built as `origin/staging` (not the old
`p1-05-cross-tenant-negative-suite` branch, which no longer exists as an open PR) merged with
`origin/surajk86808/p2-03-intake-questions-product-read` (P2-03, PR #110, which itself is based on
P2-01's branch, PR #107 — so p3-base has both). P2-02 (PR #108, admin CRUD router) intentionally
left out, per the brief's own note in section 0.2 — not a P3 dependency.

## Flagged (things that deviated from the brief, or decisions punted back to Vansh)

- **Section 0.1's PR/branch table is stale**: P1 (#114-123) is merged to `staging`, not open/stacked.
  `p3-base` = `staging` + P2-03's branch merged in, not `p1-05-...` + P2-03 as literally written.
- **P2-01's migration (`67e5302afcfe_deal_intake_questions.py`, PR #107) has a stale
  `down_revision`**: it points at `1a2b3c4d5e6f` (the pre-P1 head, from when that PR branched),
  which produces two Alembic heads once merged onto a staging that now has all of P1. Fixed locally
  on `p3-base` by re-pointing `down_revision` to `b4f8e1c3a962` (P1-03's migration, the real current
  tip). **This same fix needs to land on PR #107 itself before it's mergeable to staging** — flagging
  here since it's Suraj's PR; I didn't touch #107, only my local integration copy.
- P2-02 not in `p3-base` (per brief section 0.2) — P3-01's tests insert `deal_intake_questions` rows
  directly via the ORM/fixtures, not through the admin router.
- **P3-01's implementer found the "known gap" I'd flagged (missing partial unique index /
  `dd_public` grant) was wrong** — both already exist from P1-01
  (`ux_deal_intake_link_pending_deal`, the narrow `UPDATE` grant). No migration was needed for
  P3-01; `alembic heads` stayed single (`67e5302afcfe`) throughout. Correcting my own instructions
  here for whoever reads this next.
- **P3-01 product decision, resolved**: the 409-on-existing-`analysis_run` check uses
  `AnalysisRunRepo.latest_for_deal` (ANY status, not just active) — confirmed with Vansh
  (2026-08-27): once a deal has ever had any analysis run, even a long-finished one, it can never
  get a fresh external intake link. Kept as built, no code change.
- **Pre-existing test-isolation bug found during P3-01 (not fixed, not in scope)**:
  `tests/test_memory_scope_rls.py` and `tests/test_retrieval_rls.py` each `DROP TABLE IF EXISTS
  chunks CASCADE` at setup/teardown without recreating it, permanently dropping the real
  migration-created `chunks` table for any DB used afterward in the same run. Needs its own
  follow-up ticket outside P3 (fix: recreate in teardown, or use a savepoint/transaction instead of
  raw DDL).
- **Separate infra finding during P3-01 (not fixed, not in scope)**: the checked-in `.env`'s
  `ALEMBIC_DATABASE_URL` points at a live DigitalOcean cluster that's several migrations behind
  head — surfaced a `DuplicateObjectError` when alembic ran against it once with real `.env`
  values (rolled back clean, transactional DDL, no damage). Worth checking independently of P3.
- **Shared-checkout git race (P1 wave-0 tickets)**: P3-01 and P3-07 were initially both run as
  orchestrators directly in the main checkout (no worktree isolation), switching branches with
  `git checkout`. A status-doc commit I made raced against P3-01's own tester subagent staging
  changes at the same moment, and my commit accidentally bundled in an unintended reversion of
  P3-01's 4 implementation files. No data was lost (working tree still matched the last good
  commit byte-for-byte) — caught immediately via `git show --stat HEAD`, fixed with a follow-up
  commit restoring the 4 files. **Fix going forward**: every ticket from P3-07 onward runs in its
  own dedicated `git worktree` under `.claude/worktrees/<ticket>/`, never the shared main checkout.
- **Environment-only issue (P3-14, and hit again independently)**: the dev Postgres container's
  volume can end up with `alembic_version` claiming head while `chunks` and downstream tables are
  actually missing (stale volume, not a migration bug). Fix: `docker compose -f
  docker-compose.dev.yml down -v postgres && up -d postgres` then `alembic upgrade head`.
- **Real architectural gap found in P3-07, affects every future ticket that audits a dd_public
  action (P3-10, P3-11 next)**: `HumanAuditRepo.append()` via `session.add()` + ORM flush emits an
  implicit `INSERT ... RETURNING` to populate server-generated columns (`id`, `created_at`).
  Postgres requires `SELECT` privilege to satisfy `RETURNING` — but `dd_public`'s grant on
  `human_audit_log` (P1-00) is deliberately `INSERT`-only, no `SELECT`, since that table holds
  every org's full activity trail, not just intake events. First ticket to ever call this repo
  method from a `dd_public` session, so it never surfaced before (`dd_app` has always had
  `SELECT` there too). **Decision (confirmed with Vansh, 2026-08-27): do NOT grant dd_public
  SELECT** — RLS would scope it to one org, but a valid external link session would then be able
  to read that org's *entire* audit history, not just intake-related rows, which is broader than
  this feature needs. **Fix actually applied**: table-level `implicit_returning: False` on
  `HumanAuditLog.__table_args__` (`app/models/human_audit_log.py`) — not a per-call-site Core
  insert as I first suggested. This is a SQLAlchemy-only setting (controls whether the ORM *asks*
  Postgres for `RETURNING`), zero effect on the grant or any `WITH CHECK` policy. Verified safe for
  every existing `dd_app` caller too: every `HumanAuditRepo(...).append(...)` call site
  (`history.py`, `uploads.py`, `mandates.py`, `admin/*.py`, `jobs/tasks/*.py`) discards the return
  value, none read back `id`/`created_at` off the inserted object. **Whoever builds P3-10/P3-11
  needs nothing further here** — this is a model-level fix, already in effect for any session role.
- **`implicit_returning=False` alone wasn't sufficient (found by P3-07's implementer)**: disabling
  `RETURNING` also removed the ORM's only way to learn the server-generated UUID PK after insert,
  causing `sqlalchemy.orm.exc.FlushError: ... has a NULL identity key`. Fixed by adding a
  client-side `default=uuid.uuid4` alongside the existing `server_default=func.gen_random_uuid()`
  on `HumanAuditLog.id` — Python computes the id before the INSERT is sent, so `RETURNING` is no
  longer needed for it either. No migration needed (same column type/value shape).
- **Session/process restart mid-verification (unrelated to any ticket's code)**: the local Claude
  Code session and Docker daemon both restarted during this work (cause unclear — possibly a
  system sleep/wake or manual restart), which tore down all in-flight orchestrator subagents
  (`p3-01`, `p3-07`, `p3-14`, `intake-link-p1`) though their git commits were all durable and
  unaffected. One consequence: `tests/test_public_intake_session.py`'s
  `test_issued_session_token_is_rejected_by_decode_clerk_jwt` (found during my own re-verification
  after the restart, no live orchestrator left to hand it back to) called the real
  `decode_clerk_jwt`, which fetches Clerk's JWKS over a live httpx call before checking anything
  else — fails in any environment without a real, reachable Clerk JWKS endpoint (mine included).
  `tests/test_intake_session_jwt.py` already documented this exact tradeoff for the reverse
  direction and chose a structural argument over a live network dependency; fixed by bringing the
  P3-07 test in line with that same precedent (commit `ace8c6f`) rather than reintroducing the
  fragility it deliberately avoided.
- **P3-12 open items, not resolved, need a decision before P3-08/P3-09/P3-11 land**: (1) per-`link_id`
  throttling on session-authenticated routes (ticket text mentions it) was deliberately NOT built —
  no route uses `get_public_session_db` yet (P3-08 through P3-11 don't exist), so there was nothing
  real to throttle or test against. Recommendation on record: enforce it inside
  `get_public_session_db` itself once those routes exist (it already has `claims.link_id`
  post-decode). (2) The IP-throttle window (5 requests / 10s) is numerically pinned to the lockout
  threshold (5) so a hammering script against one link always hits the throttle before the lockout —
  but once more `/api/public` routes exist, a legitimate multi-step form fill from one IP may need a
  looser window. Not a live problem today (only `/session` exists), but flagging so it isn't
  forgotten before P3-13's audit.
- **Self-review of PR #136/#137/#138/#139 (2026-08-28) — two fixes applied, three questions
  punted back to Vansh:**
  - **Fixed**: `create_intake_session` (`app/api/public_intake.py`) compared the submitted
    email to `link.recipient_email` with `!=` — non-constant-time on attacker-controlled
    input, on the one public unauthenticated endpoint in this feature. Swapped to
    `hmac.compare_digest` on the normalized (lowercased) forms. Commit `b12e908` on
    `p3-07-intake-session-endpoint`, merged forward (not rebased) into `p3-12-rate-limiting`
    at `73ce4a0`. Regression test added
    (`test_mismatch_still_404s_and_bumps_attempt_with_constant_time_compare`); the two
    pre-existing tests that pin the mismatch path's behavior
    (`test_wrong_email_404s_bumps_failed_attempt_and_audits`,
    `test_byte_identical_404_across_every_failure_mode`) still pass unmodified.
  - **Fixed**: `RateLimitMiddleware.dispatch`'s fail-open behavior on
    `redis.exceptions.RedisError` (deliberate — a Valkey outage must not take down the whole
    public intake surface) had no test coverage, so a future refactor could silently flip it
    to fail-closed or drop the `except` clause with nothing to catch it. Added
    `test_valkey_error_fails_open_not_429_not_500` to `tests/test_rate_limit_middleware.py`
    (monkeypatches `check_rate_limit` to raise, asserts the request still reaches the route).
    Commit `edb37d0` on `p3-12-rate-limiting`.
  - **Open question 1 — shared IP-throttle budget across the whole `/api/public/intake/*`
    prefix**: `IP_LIMIT=5`/`IP_WINDOW_SECONDS=10` throttles every route under the prefix, not
    just `/session` (same underlying concern already on record two bullets up, from P3-12's
    own open items). Once P3-08 (uploads) and other P3 routes land under this prefix, a
    legitimate applicant doing a multi-file upload could plausibly trip it. **Not changed.**
    Needs Vansh to confirm whether the shared budget is intentional or whether `/session`
    should get its own tighter limit with other routes on a separate/looser one.
  - **Open question 2 — `_client_ip()` trusting the last `X-Forwarded-For` entry**: correct
    only if the app container is genuinely unreachable except through Caddy. This is an
    infra-level fact this session cannot verify from code. **Not changed.** Needs a human
    confirmation that no other ingress path exists in prod/staging — if one does, the per-IP
    throttle is trivially bypassable by spoofing that header.
  - **Open question 3 — per-link lockout has no automated unlock**: once
    `failed_attempts >= 5`, the link 404s forever, even for the correct email. Presumed
    intended recovery path is "the deal owner reissues a new link via P3-01"
    (`POST /api/deals/{deal_id}/intake-link`, which lazy-expires/replaces a stale pending
    link). **Not changed, not confirmed.** Needs Vansh to confirm this is the intended
    recovery path so a future support request isn't mistaken for a bug.
  - **CI gap, recurring — root cause found**: PR #138 and #139 showed no CI check-runs at
    all on their pre-fix heads (`5c9019d`, `4aba508`) — not pending, not queued, never
    triggered (only a queued DigitalOcean deploy check-suite existed), while #136/#137 both
    had full green CI. Same pattern as #110 earlier. Root cause: `.github/workflows/ci.yml`'s
    `on.pull_request.branches` is `[main, staging]` — GitHub's `branches` filter matches the
    PR's **base** branch, and it only fires there. #136/#137 are both based on `staging`, so
    CI runs normally. #138 (base `p3-01-intake-link-generate`) and #139 (base
    `p3-07-intake-session-endpoint`) are stacked PRs based on other feature branches, so CI
    structurally never triggers on them — confirmed empirically: pushing Fix 2 to #139
    (`edb37d0`) produced zero workflow runs, and a trivial empty commit
    (`git commit --allow-empty`) pushed to #138 (`35d546f`) also produced zero. This is not a
    flake and an empty-commit retrigger cannot fix it. **Not changed** — the fix (adding the
    stacked branches to the trigger, or switching to `pull_request_target`/a
    `workflow_run`-based re-trigger, or just re-basing/re-targeting these two PRs at `staging`
    once their upstream PRs merge) is a CI-config decision for Vansh, not something to
    silently pick here. Until resolved, #138 and #139 cannot be verified as green by CI and
    should not be treated as merge-ready on the diff alone.
  - **CI infra gap, found while chasing #137's red CI**: `.github/workflows/ci.yml`'s `test`
    job (~line 88) defines a `postgres` service container but no `valkey`/redis one, even
    though it sets `VALKEY_URL: "redis://localhost:6379/0"` (~line 119) as if one exists.
    Latent since P1 — first exposed now because PR #139 (P3-12, Valkey-backed rate limiting)
    was squash-merged into `p3-07-intake-session-endpoint` (PR #137's branch), and its tests
    (`tests/conftest.py::clear_rate_limit_keys`, `tests/test_public_intake_session.py`'s new
    autouse `_clear_rate_limit_keys_around_every_test`, `tests/test_rate_limit_middleware.py`)
    are the first in this repo to need Valkey reachable during `uv run pytest` in CI. Confirmed
    via the failing run: `redis.exceptions.ConnectionError` on `localhost:6379`, `--maxfail=1`
    stopping the job after 524/~780 passed. **Fixed (2026-08-28, commit `dd971c8` on
    `p3-07-intake-session-endpoint`)**: added a `valkey` service block to the `test` job,
    mirroring the `postgres` block (image `valkey/valkey:8`, `6379:6379`, health-cmd
    `valkey-cli ping`). Verified: PR #137's CI is fully green (`gh pr checks 137`), including
    `Test (pytest)`.
  - **PR #137 merge conflict with `staging`, found immediately after the CI fix (2026-08-28)**:
    pushing the CI fix alone left the PR non-mergeable — `staging` had moved 5 commits ahead
    (PR #136/P3-01 itself merged, plus SIM-262 entity verification, SIM-420/421 corroboration
    adapters, PR #140's deploy wiring) since P3-07 branched. Two real conflicts on merge:
    `app/repo/IntakeLinkRepo.py` (both sides purely additive — P3-07's `bump_failed_attempt` vs.
    staging's `get_pending_for_deal`/`get_pending_for_deal_unlocked`/`mark_expired` from P3-14 —
    resolved by keeping all four methods and the union of imports) and
    `docs/plans/external-deal-intake-link-p3-status.md` itself (add/add — P3-07's branch carried
    a stale, pre-P3-01 snapshot of this file; resolved by taking this repo's current copy
    wholesale rather than reconciling two outdated forks). Merge commit `7dbad8f`. Re-verified
    after merge: `alembic heads` single (`76a165315331`), pyright 0 errors, full suite 964/964
    passed (excluding two confirmed-pre-existing, confirmed-unrelated local artifacts — see next
    two bullets) on a fresh dev Postgres volume. Pushed; PR #137 mergeable, CI green again on the
    merge commit (run `33134168781`).
  - **Two known-environment test failures hit again while re-verifying #137 post-merge, both
    already documented above, confirmed not regressions**: (1)
    `tests/test_public_intake_pool.py::test_missing_public_database_url_raises_validation_error`
    fails locally only because the checked-in `.env` has `PUBLIC_DATABASE_URL` set and
    `Settings()` reads it directly, bypassing the test's `monkeypatch.delenv` — pre-existing
    since P1-07 (`git blame` confirms), passes in CI (no `.env` file there). (2) the
    `DROP TABLE chunks CASCADE`-without-recreate bug in `test_memory_scope_rls.py`/
    `test_retrieval_rls.py` again polluted `test_chunks_rls.py`/`test_e2e_pipeline.py`/
    `test_l2_retrieval_eval.py` when run in the same session — confirmed non-regression by
    running the affected files in isolation on a fresh volume (15/15 passed).
    stopping the job after 524/~780 passed. **Fixed (2026-08-28, commit `dd971c8` on
    `p3-07-intake-session-endpoint`)**: added a `valkey` service block to the `test` job,
    mirroring the `postgres` block (image `valkey/valkey:8`, `6379:6379`, health-cmd
    `valkey-cli ping`). Verified: PR #137's CI is fully green (`gh pr checks 137`), including
    `Test (pytest)`.
  - **PR #137 merge conflict with `staging`, found immediately after the CI fix (2026-08-28)**:
    pushing the CI fix alone left the PR non-mergeable — `staging` had moved 5 commits ahead
    (PR #136/P3-01 itself merged, plus SIM-262 entity verification, SIM-420/421 corroboration
    adapters, PR #140's deploy wiring) since P3-07 branched. Two real conflicts on merge:
    `app/repo/IntakeLinkRepo.py` (both sides purely additive — P3-07's `bump_failed_attempt` vs.
    staging's `get_pending_for_deal`/`get_pending_for_deal_unlocked`/`mark_expired` from P3-14 —
    resolved by keeping all four methods and the union of imports) and
    `docs/plans/external-deal-intake-link-p3-status.md` itself (add/add — P3-07's branch carried
    a stale, pre-P3-01 snapshot of this file; resolved by taking this repo's current copy
    wholesale rather than reconciling two outdated forks). Merge commit `7dbad8f`. Re-verified
    after merge: `alembic heads` single (`76a165315331`), pyright 0 errors, full suite 964/964
    passed (excluding two confirmed-pre-existing, confirmed-unrelated local artifacts — see next
    two bullets) on a fresh dev Postgres volume. Pushed; PR #137 mergeable, CI green again on the
    merge commit (run `33134168781`).
  - **Two known-environment test failures hit again while re-verifying #137 post-merge, both
    already documented above, confirmed not regressions**: (1)
    `tests/test_public_intake_pool.py::test_missing_public_database_url_raises_validation_error`
    fails locally only because the checked-in `.env` has `PUBLIC_DATABASE_URL` set and
    `Settings()` reads it directly, bypassing the test's `monkeypatch.delenv` — pre-existing
    since P1-07 (`git blame` confirms), passes in CI (no `.env` file there). (2) the
    `DROP TABLE chunks CASCADE`-without-recreate bug in `test_memory_scope_rls.py`/
    `test_retrieval_rls.py` again polluted `test_chunks_rls.py`/`test_e2e_pipeline.py`/
    `test_l2_retrieval_eval.py` when run in the same session — confirmed non-regression by
    running the affected files in isolation on a fresh volume (15/15 passed).
  - **PgBouncer prod config drift from the `docker/pgbouncer.ini` reference file, found while
    checking "is there another DO-connection-limit-shaped issue" (2026-08-28)**:
    `docker/pgbouncer.ini` (P1-07's design) gives `dd_public` its own **named** `[databases]`
    entry (`simpero_public = ... pool_size=5`) so the low-trust public intake path can't
    starve `dd_app`'s pool, and raises `max_client_conn` 22 → 40 to make room for it.
    `docker-compose.prod.yml` — the file actually deployed to the droplet (inlines its own
    PgBouncer config via heredoc rather than mounting `docker/pgbouncer.ini`, since only that
    file + Caddyfile + `.env` live on the droplet) — never adopted that design.
    PR #140 (`d7c2378`, merged 2026-08-27 by Kuntal, `Deploy: wire dd_public + intake settings
    into staging/prod`) fixed the actually-crashing bug (deploy.yml never wrote
    `PUBLIC_DATABASE_URL`/`DD_PUBLIC_PASSWORD`/`INTAKE_SESSION_JWT_SECRET` to the droplet
    `.env`, so `Settings()` failed on import — every deploy since P1 merged was dying before
    running migrations) by adding `dd_public` to the userlist, same flat treatment as
    `dd_app` — but did **not** give it the dedicated named entry/smaller pool, and left
    `max_client_conn` at `22`. So in production today, `dd_public` connections fall through
    to the wildcard `*` entry and share `dd_app`'s pool budget, the exact starvation risk
    P1-07's design was meant to prevent. **Fixed (2026-08-28, explicitly approved by Vansh) —
    [PR #149](https://github.com/Simpero-AI/Simpero_AI_Gov_Alpha/pull/149)**, branched off
    `staging` (not bundled into `p3-base`/P3 work, since this is an independent infra fix):
    added the `simpero_public` named entry (`pool_size=5`) and raised `max_client_conn` 22 → 40
    in `docker-compose.prod.yml`'s inline heredoc, matching `docker/pgbouncer.ini` exactly.
    YAML validated (`python3 -c "import yaml; ..."`), not deployed/tested against the live
    droplet by this session — PR left open for review, not merged. **Depends on a fact this
    session cannot verify**: the `PUBLIC_DATABASE_URL` GitHub Actions secret (written to the
    droplet `.env` by `deploy.yml`) must use `dbname=simpero_public` (matching `.env.example`'s
    documented shape), not `simpero` — otherwise `dd_public` traffic keeps hitting the wildcard
    entry regardless of this config change, silently. Worth Vansh double-checking that secret's
    value before/after the next deploy.
  - **Original DO `max_connections` confirmation (P1-07) — resolved (2026-08-28, via `doctl`,
    which was already authenticated in this environment)**: `SHOW max_connections` on the live
    cluster (`db-pgsql-tor1-13122`, plan `db-s-1vcpu-1gb`) returns **30**. Breakdown of what
    draws from that budget: (1) Postgres's own internal auxiliary processes (autovacuum
    launcher, checkpointer, walwriter, walsender, background writer, logical-replication
    launcher, `pg_cron`/TimescaleDB/failover-slots workers) — confirmed via
    `backend_type` in `pg_stat_activity`, these do **not** count as `client backend` connections
    and are not the constraint. (2) Real client-backend connections, which **are** the
    constraint: at check time, 3 — one `dd_app`, one `doadmin`, one DO-internal
    (`management-agent`/`pghoard` backup tooling). (3) The pool-split design above's
    **theoretical ceiling**: `default_pool_size=20` (dd_app, shared by `app` + `worker`, since
    both route through the same PgBouncer instance/pool) + `pool_size=5` (dd_public) = **25**
    backend connections PgBouncer could open simultaneously under full saturation. Against a
    30-connection cap, that leaves only **~5** of real slack for `doadmin`'s direct (PgBouncer-
    bypassing) Alembic migration connections, DO's own client-backend tooling, and any other
    role on this cluster (a `kp_db` user exists in the cluster's user list — **not referenced
    anywhere in this repo**, purpose unknown to this session, flagging rather than assuming
    it's inert). **Verdict: 30 technically fits the design's ceiling today, but the margin is
    thin, not comfortable** — current real load (3 client-backend connections) is nowhere near
    it, so there's no live problem, but this doesn't have much room to grow before a coinciding
    migration + backup + traffic spike could exhaust it. Worth deciding with Vansh: bump the DO
    plan size for real headroom, or trim `default_pool_size` down from 20 if the app's actual
    concurrent DB usage doesn't need it. Both this and the `docker-compose.prod.yml` fix above
    were decided together, per this section's own earlier note.
  - **P3-10 data-model gap, resolved via architect decision (2026-08-28)**: `data_source` had
    no column tying a row to a specific `deal_intake_link` — only `deal_id`/`org_id`. The
    ticket's "20-file-per-link ceiling" could have been approximated by counting `data_source`
    rows scoped to `deal_id` (no schema change), but that would let org-side uploads through
    the normal authenticated `/api/uploads/*` path — which P3-14 does **not** block while a
    link is pending, only `start_analysis` — eat into the external recipient's own upload
    budget, producing confusing 409s for a legitimate applicant. Added a real
    `intake_link_id` column instead (migration `9a48cce5ecac`) — see the P3-10 row below and
    its implementation doc for the full detail, including the advisory-lock concurrency
    approach for the presign/complete TOCTOU race.
  - **P3-10 branch rebuilt off `origin/staging` directly, not `p3-base`, before opening its
    PR (2026-08-28)** — same reasoning as PR #149: the `.claude/worktrees/p3-10` branch was
    created off `p3-base`, which carries 19 local-only commits (Suraj's unmerged P2-01/P2-03
    work plus doc-tracking commits) that would otherwise show up in the PR diff. Cherry-picked
    the single P3-10 commit onto a fresh branch off `origin/staging` (which had also picked up
    PR #147/SIM-422 in the meantime), re-verified clean there (single Alembic head, pyright 0
    errors, full suite 1020/1020), and force-pushed over the already-pushed
    `p3-10-public-uploads` remote branch before opening PR #150. If you're the one opening a
    PR for a ticket branch built off `p3-base`, check whether `p3-base` has drifted from
    `origin/staging` first — it will, since it accumulates status-doc commits continuously.
  - **Session-collision incident during P3-10 (2026-08-28, caught and contained, no damage)**:
    this session initially fire-and-forget spawned a single `orchestrator` subagent (named
    `p3-10-orchestrator`) to build the whole ticket, then — per Vansh's explicit instruction
    ("you be the orchestrator") — stopped it (`TaskStop`, confirmed successful) and drove the
    architect → implementer pipeline directly instead. Partway through the implementer's run,
    `ListAgents` showed a **second** `p3-10-orchestrator` entry in state `running`, started
    well after the stop — same task id (`ae13a7c86a7c950ea`) as the one already stopped, which
    per this tool's own semantics means something sent it a new message and resumed it (this
    session did not). Stopped it again immediately (before it could write anything — confirmed
    via `git status` showing no new/changed files beyond what the implementer had already
    produced) and did not investigate further mid-task. **Not fully explained** — worth
    Vansh checking whether this was an accidental resume (e.g. a stray message from another
    tool/session) before relying on background-orchestrator fire-and-forget delegation again
    without watching for this.
  - **Standing rule added mid-P3 (2026-08-28)**: Vansh asked that any architectural decision —
    made by this session or by a subagent — get his explicit sign-off before implementation
    starts, not just a confident architect recommendation. Applied starting with P3-09's
    draft-answers persistence decision (see that ticket's row) — presented the tradeoffs,
    waited for approval, then spawned the implementer. Saved as a standing memory
    (`architectural-decisions-need-approval`) for future sessions. P3-10's `intake_link_id`
    column decision (earlier the same day) was NOT gated this way — done before the rule was
    stated, not revisited retroactively.
  - **P3-11 completeness gate, confirmed with Vansh (2026-08-28)**: P3-11's own ticket text/AC
    never mentions blocking submit on unanswered required questions — only the "≥1
    verified-or-pending `data_source`" gate is specified. Flagged as a real spec gap (not a
    P3-09 question) rather than silently guessing. **Vansh confirmed: yes, all required
    questions must be answered before submit succeeds** — P3-11's implementation must add this
    gate explicitly; it is not covered by anything P3-09 already enforces (P3-09 only validates
    the keys present in a given call, never the whole required set).
  - **P3-11 document-gate scope, approved by Vansh (2026-08-28)**: same spec-gap family as
    P3-10's — ticket text says "≥1 ... `data_source` for the deal," literally `deal_id`-scoped,
    but built `intake_link_id`-scoped instead (new `DataSourceRepo.count_for_intake_link_by_status`).
    A `deal_id`-scoped count would let an org-side authenticated upload
    (never blocked while a link is pending) satisfy an external recipient's own upload
    requirement with zero uploads of their own — defeats the AC's actual intent. Tested
    explicitly (`test_submit_document_gate_ignores_other_links_and_authenticated_uploads`), not
    just trusted.
  - **P3-11 submit-sequencing/locking, approved by Vansh (2026-08-28) — a genuine concurrency
    design decision, not spec-pinned**: traced `intake_response_insert`'s `WITH CHECK`
    (`b4f8e1c3a962`) directly rather than assuming P3-09's 404-pattern would transfer unchanged
    — it requires the link still `status = 'pending'` **at the moment the response row is
    inserted**, so the response must be written *before* the status flip to `submitted`. That
    ordering alone reopens the exact race the AC warns about ("fails closed... rather than
    duplicating the response row"): two concurrent `/submit` calls could both read `pending`
    before either writes, both pass validation, both insert a response row. Fixed with a
    `SELECT ... FOR UPDATE` row lock on the link as the very first step (new
    `IntakeLinkRepo.get_pending_by_id_for_update`, same idiom as `get_pending_for_deal`'s
    reissue-race lock) — a second concurrent call blocks until the first commits, then sees
    `submitted` and 404s cleanly. Test coverage proves "only one response row after two
    sequential calls," not true concurrent-connection coverage (same tradeoff already made for
    P3-10's advisory lock — this test suite has no infrastructure for genuinely concurrent DB
    connections).
  - **`deal_intake_response` needed the `implicit_returning=False` fix too, found during P3-11
    (2026-08-28)**: same gap as `HumanAuditLog` (P3-07's Flagged entry above) — `dd_public` is
    INSERT-only, no SELECT, on `deal_intake_response` (deliberate, per that table's own
    migration docstring), and Postgres requires SELECT to satisfy SQLAlchemy's default
    RETURNING clause. This table had never actually been written to by any ticket before P3-11,
    so the gap was latent until now. Fixed the same way: `implicit_returning: False` in
    `__table_args__` + client-side `default=uuid.uuid4` on `id`, alongside the existing
    `server_default=func.gen_random_uuid()`.
  - **Two SQLAlchemy/`AsyncSession` subtleties found empirically during P3-11, both load-
    bearing, not style choices**: (1) `db.add(response)` written before `link.status = ...` in
    Python code does **not** guarantee the INSERT is emitted before the status UPDATE at the SQL
    level — SQLAlchemy's unit-of-work doesn't preserve call order across unrelated ORM objects,
    so an explicit `await db.flush()` right after `db.add(response)` is required to force the
    ordering `intake_response_insert`'s `WITH CHECK` needs (see the locking decision above).
    (2) `func.now()` assigned to an ORM attribute for an **UPDATE** doesn't auto-populate the
    Python-side value the way it does for an INSERT (no RETURNING is emitted) — reading
    `link.submitted_at` right after flush raises `MissingGreenlet` (an implicit lazy-load
    outside the async greenlet). Fixed with an explicit `await db.refresh(link,
    attribute_names=["submitted_at"])` before building the response.
  - **P3-11's PR has no clean single base — a genuine diamond dependency**: the branch merges
    `p3-09-public-answers` (based on P3-08's own commit) and `p3-10-public-uploads` (based on
    `origin/staging` directly) — neither branch is an ancestor of the other, both only share
    `origin/staging` as a common ancestor. GitHub PRs support one base branch; whichever is
    picked, the diff includes the *other* branch's commits too (already under separate review
    in PRs #151/#150/#153). Opened against `p3-10-public-uploads` as base, with the PR
    description calling out exactly which commits are net-new to this PR (the Alembic merge
    migration + P3-11's own implementation commit) so a reviewer isn't confused into
    re-reviewing already-covered work. **P3-13, the next ticket, will hit the same problem at
    larger scale** — its own ticket text already anticipates this ("Branch off a merge of all
    five... expect this branch to need the most merge attention of anything in P3").

## Tickets

| Ticket | Owner | Status | Branch | Based on | Pushed? | Tested against | Notes |
|---|---|---|---|---|---|---|---|
| P3-01 | Vansh | **Merged to staging** | `p3-01-intake-link-generate` | staging | **Yes — [PR #136](https://github.com/Simpero-AI/Simpero_AI_Gov_Alpha/pull/136)**, merged 2026-08-28 (`829af53`) | Real Postgres (dev, port 5434) | Shared effective-status helper: `app/services/intake_links.py::compute_intake_link_effective_status(link) -> str` — P3-02/06/14 import this. 20/20 new tests + full suite (789/789) + pyright 0 errors. CI green throughout. |
| P3-01 | Vansh | **Merged to staging** | `p3-01-intake-link-generate` | staging | **Yes — [PR #136](https://github.com/Simpero-AI/Simpero_AI_Gov_Alpha/pull/136)**, merged 2026-08-28 (`829af53`) | Real Postgres (dev, port 5434) | Shared effective-status helper: `app/services/intake_links.py::compute_intake_link_effective_status(link) -> str` — P3-02/06/14 import this. 20/20 new tests + full suite (789/789) + pyright 0 errors. CI green throughout. |
| P3-05 | Suraj | | | p3-base | | | |
| P3-07 | Vansh | **Merged to staging** | `p3-07-intake-session-endpoint` (worktree `.claude/worktrees/p3-07`) | staging | **Yes — [PR #137](https://github.com/Simpero-AI/Simpero_AI_Gov_Alpha/pull/137)**, merged 2026-08-28 (`bf0f6f4`) | Real Postgres (dev, port 5434), fresh volume | Commits `c3af65b` (impl) + `ace8c6f` (test fix). `human_audit_log` insert from a `dd_public` session needed `implicit_returning=False` + a client-side `default=uuid.uuid4` on `HumanAuditLog.id` (see Flagged). Full suite 773/774 (1 pre-existing unrelated failure), pyright 0 errors. **Self-review fix (2026-08-28, commit `b12e908`)**: email comparison switched to `hmac.compare_digest` (constant-time) — see Flagged. **CI fix (2026-08-28, commit `dd971c8`)**: added missing `valkey` service to CI's `test` job — see Flagged. **Merged `staging` in (2026-08-28, commit `7dbad8f`)** to pick up #136 + entity-verification work + PR #140's deploy wiring and resolve the resulting merge conflict — see Flagged. CI fully green post-merge (run `33134168781`), full suite 964/964 (excluding two confirmed-pre-existing local-only artifacts). Merged to `staging` by Vansh shortly after CI went green — merge itself happened out-of-band, not by this session. |
| P3-10 | Vansh (picked up from Suraj — unblocks P3-15/11/13, Suraj hadn't started wave-0 yet) | Done | `p3-10-public-uploads` (worktree `.claude/worktrees/p3-10`) | staging (rebuilt clean off `origin/staging` directly, not `p3-base` — see Flagged, avoids dragging in `p3-base`'s unrelated local-only commits) | **Yes — [PR #150](https://github.com/Simpero-AI/Simpero_AI_Gov_Alpha/pull/150)** | Real Postgres (dev, port 5434), fresh volume | New `data_source.intake_link_id` column (migration `9a48cce5ecac`, down_revision `76a165315331`) — architect decision, see Flagged: `DataSource` had no per-link column, only `deal_id`/`org_id`, and a deal-scoped count would let org-side authenticated uploads eat into the external recipient's 20-file ceiling. Tightened `intake_deal_documents_insert`'s `WITH CHECK` to require the inserted row's `intake_link_id` match `app.intake_link_id`. `DataSourceRepo.try_create_for_intake_link` uses `pg_advisory_xact_lock` keyed on link id to close the presign-then-complete TOCTOU race (presign's own check is a UX courtesy only). New `app/api/public_uploads.py`, first real usage of `get_public_session_db` in this codebase. 9/9 new tests + full suite 1020/1020 (rebuilt onto `origin/staging` after SIM-422 merged; excluding known pre-existing environment artifacts) + pyright 0 errors. Implementation doc: `docs/implementations/2026-08-28-p3-10-public-uploads.md`. **Flagged for product confirmation**: SAQ `ingest_data_source` enqueue on `/complete` wasn't explicit in the ticket text, added because "byte-for-byte identical `data_source` row" implies the same downstream processing — double-check this is actually wanted. |
| P3-07 | Vansh | **Merged to staging** | `p3-07-intake-session-endpoint` (worktree `.claude/worktrees/p3-07`) | staging | **Yes — [PR #137](https://github.com/Simpero-AI/Simpero_AI_Gov_Alpha/pull/137)**, merged 2026-08-28 (`bf0f6f4`) | Real Postgres (dev, port 5434), fresh volume | Commits `c3af65b` (impl) + `ace8c6f` (test fix). `human_audit_log` insert from a `dd_public` session needed `implicit_returning=False` + a client-side `default=uuid.uuid4` on `HumanAuditLog.id` (see Flagged). Full suite 773/774 (1 pre-existing unrelated failure), pyright 0 errors. **Self-review fix (2026-08-28, commit `b12e908`)**: email comparison switched to `hmac.compare_digest` (constant-time) — see Flagged. **CI fix (2026-08-28, commit `dd971c8`)**: added missing `valkey` service to CI's `test` job — see Flagged. **Merged `staging` in (2026-08-28, commit `7dbad8f`)** to pick up #136 + entity-verification work + PR #140's deploy wiring and resolve the resulting merge conflict — see Flagged. CI fully green post-merge (run `33134168781`), full suite 964/964 (excluding two confirmed-pre-existing local-only artifacts). Merged to `staging` by Vansh shortly after CI went green — merge itself happened out-of-band, not by this session. |
| P3-10 | Vansh (picked up from Suraj — unblocks P3-15/11/13, Suraj hadn't started wave-0 yet) | Done | `p3-10-public-uploads` (worktree `.claude/worktrees/p3-10`) | staging (rebuilt clean off `origin/staging` directly, not `p3-base` — see Flagged, avoids dragging in `p3-base`'s unrelated local-only commits) | **Yes — [PR #150](https://github.com/Simpero-AI/Simpero_AI_Gov_Alpha/pull/150)** | Real Postgres (dev, port 5434), fresh volume | New `data_source.intake_link_id` column (migration `9a48cce5ecac`, down_revision `76a165315331`) — architect decision, see Flagged: `DataSource` had no per-link column, only `deal_id`/`org_id`, and a deal-scoped count would let org-side authenticated uploads eat into the external recipient's 20-file ceiling. Tightened `intake_deal_documents_insert`'s `WITH CHECK` to require the inserted row's `intake_link_id` match `app.intake_link_id`. `DataSourceRepo.try_create_for_intake_link` uses `pg_advisory_xact_lock` keyed on link id to close the presign-then-complete TOCTOU race (presign's own check is a UX courtesy only). New `app/api/public_uploads.py`, first real usage of `get_public_session_db` in this codebase. 9/9 new tests + full suite 1020/1020 (rebuilt onto `origin/staging` after SIM-422 merged; excluding known pre-existing environment artifacts) + pyright 0 errors. Implementation doc: `docs/implementations/2026-08-28-p3-10-public-uploads.md`. **Flagged for product confirmation**: SAQ `ingest_data_source` enqueue on `/complete` wasn't explicit in the ticket text, added because "byte-for-byte identical `data_source` row" implies the same downstream processing — double-check this is actually wanted. |
| P3-02 | Suraj | | | P3-01 branch | | | |
| P3-03 | Suraj | | | P3-01 branch | | | |
| P3-06 | Suraj | | | P3-01 branch | | | |
| P3-14 | Vansh | Done | `p3-14-analysis-gate` (worktree `.claude/worktrees/p3-14`) | P3-01's PR branch | **Yes — [PR #138](https://github.com/Simpero-AI/Simpero_AI_Gov_Alpha/pull/138)**, stacked on #136 | Real Postgres (dev, port 5434) | New `IntakeLinkRepo.get_pending_for_deal_unlocked` (deliberately unlocked — reusing the locked `get_pending_for_deal` would serialize `start_analysis` against concurrent link generate/submit). Guard clause in `start_analysis` right after the existing `active_for_deal` 409. 46/46 tests (5 new + regression) + full suite 794/794 + pyright 0 errors. **CI never triggers on this PR** — base is `p3-01-intake-link-generate`, not `main`/`staging` (see Flagged); empty retrigger commit `35d546f` confirmed zero workflow runs. |
| P3-08 | Vansh (picked up from Suraj) | Done | `p3-08-public-questions` (worktree `.claude/worktrees/p3-08`) | staging (rebuilt clean off `origin/staging` directly, same reasoning as P3-10/PR#149) | **Yes — [PR #151](https://github.com/Simpero-AI/Simpero_AI_Gov_Alpha/pull/151)** | Real Postgres (dev, port 5434), fresh volume | `GET /api/public/intake/questions`, session-authenticated via `get_public_session_db`. Returns the link's frozen `questions_snapshot["questions"]` (sorted defensively by `display_order`) plus `org_name` — nothing else, per the ticket's literal "no field beyond what's explicitly allowed" criterion. Null `questions_snapshot` (type allows it, real data never produces it) returns `questions: []`, not a 500. Reused `_org_name_for_link`'s column-scoped-select pattern (duplicated from P3-10's `public_uploads.py`, not imported — that module isn't merged onto this branch). Found + fixed a real test fragility: two other test files seed the same shared test org under a different name with no teardown, order-dependent — this ticket's tests now assert against the live name, not a hardcoded one. 6/6 new tests + full suite 1017/1017 (two independent fresh-volume runs) + pyright 0 errors. No architectural decision needed — fully spec-pinned. |
| P3-08 | Vansh (picked up from Suraj) | Done | `p3-08-public-questions` (worktree `.claude/worktrees/p3-08`) | staging (rebuilt clean off `origin/staging` directly, same reasoning as P3-10/PR#149) | **Yes — [PR #151](https://github.com/Simpero-AI/Simpero_AI_Gov_Alpha/pull/151)** | Real Postgres (dev, port 5434), fresh volume | `GET /api/public/intake/questions`, session-authenticated via `get_public_session_db`. Returns the link's frozen `questions_snapshot["questions"]` (sorted defensively by `display_order`) plus `org_name` — nothing else, per the ticket's literal "no field beyond what's explicitly allowed" criterion. Null `questions_snapshot` (type allows it, real data never produces it) returns `questions: []`, not a 500. Reused `_org_name_for_link`'s column-scoped-select pattern (duplicated from P3-10's `public_uploads.py`, not imported — that module isn't merged onto this branch). Found + fixed a real test fragility: two other test files seed the same shared test org under a different name with no teardown, order-dependent — this ticket's tests now assert against the live name, not a hardcoded one. 6/6 new tests + full suite 1017/1017 (two independent fresh-volume runs) + pyright 0 errors. No architectural decision needed — fully spec-pinned. |
| P3-12 | Vansh | Done | `p3-12-rate-limiting` (worktree `.claude/worktrees/p3-12`) | P3-07's PR branch | **Yes — [PR #139](https://github.com/Simpero-AI/Simpero_AI_Gov_Alpha/pull/139)**, stacked on #137 | Real Postgres + Valkey (dev, port 5434/6381), fresh volume | Commit `565d737`. Part A: `failed_attempts >= 5` lockout in `public_intake.py`, same 404 body. Part B: new `app/core/rate_limit_middleware.py`, Valkey-backed IP throttle (5 req/10s, `SET NX EX` + `INCR`, keyed `ratelimit:ip:{ip}`, reuses `get_queue().redis` — no new dependency), registered before `CORSMiddleware` (traced Starlette's middleware stack build to confirm ordering keeps CORS outermost). Trusts the last `X-Forwarded-For` entry (Caddy appends, doesn't trust client-supplied header; app container has no published ports per `docker-compose.prod.yml`). Fails open on Valkey errors. 429 (not 404) for throttling — distinct signal from the 404-only contract. Per-link_id throttling deferred (see Flagged — no session-authenticated route exists yet). 9/9 new tests + full suite 778/778 + pyright 0 errors. Implementation doc: `docs/implementations/2026-08-27-p3-12-rate-limiting.md`. **Self-review fixes (2026-08-28)**: merged forward Fix 1 from #137 (`73ce4a0`, not rebased — PR already open); added fail-open regression test `test_valkey_error_fails_open_not_429_not_500` (commit `edb37d0`). Full suite re-verified on a fresh DB volume: 780/780, pyright 0 errors. **CI never triggers on this PR** — base is `p3-07-intake-session-endpoint`, not `main`/`staging` (see Flagged); confirmed after pushing `edb37d0`, zero workflow runs. |
| P3-15 | Suraj | | | P3-10 branch | | | |
| P3-09 | Vansh (picked up from Suraj — was building toward unblocking P3-11) | Done | `p3-09-public-answers` (worktree `.claude/worktrees/p3-09`) | `p3-08-public-questions` (P3-08's own commit — P3-09 genuinely needs P3-08's code, not just `origin/staging`) | **Yes — [PR #153](https://github.com/Simpero-AI/Simpero_AI_Gov_Alpha/pull/153)**, stacked on #151 | Real Postgres (dev, port 5434), fresh volume | New `deal_intake_link.draft_answers` column (migration `2f7e83611f52`, down_revision `76a165315331`) — architect decision, approved by Vansh before build, see Flagged: `deal_intake_response` is INSERT-only for `dd_public`, no place to hold in-progress drafts, so a new narrow-grant column was added rather than Valkey (breaks the "provable at DB/RLS level" invariant) or a stateless P3-11-carries-the-body redesign (relocates persistence to the client, worse for a resumed session). `IntakeLinkRepo.update_draft_answers` verified (not assumed) that a stale call against a non-pending link 404s via `dd_public`'s `intake_link_status_update` RLS policy matching zero rows — not the one-way-status trigger, which never fires for an update the RLS policy already blocked. `POST /answers` does a read-merge-write, validating only the keys present in each call (partial/progressive saves — "editable by repeated calls" would otherwise fail every call but the last). 10/10 new tests + full suite 1027/1027 (fresh volume) + pyright 0 errors (fixed 2 real `reportOptionalSubscript` errors in the implementer's test file during review — not just a stale-diagnostic false alarm this time). Implementation doc: `docs/implementations/2026-08-28-p3-09-public-answers.md`. |
| P3-11 | Vansh | Done | `p3-11-submit` (worktree `.claude/worktrees/p3-11`) | merge of `p3-09-public-answers` + `p3-10-public-uploads` + new Alembic merge migration `1dcfa5bd613d` | **Yes — [PR #154](https://github.com/Simpero-AI/Simpero_AI_Gov_Alpha/pull/154)**, base `p3-10-public-uploads` (diamond-dependency, see Flagged) | Real Postgres (dev, port 5434), fresh volume | `POST /api/public/intake/submit` — writes `deal_intake_response` from `link.draft_answers`, requires ≥1 uploaded document, flips link to `submitted`, audit row. Two architect decisions, both approved by Vansh before build (see Flagged): (1) document-count gate scoped by `intake_link_id`, not `deal_id` as the ticket text literally says — same spec-gap family as P3-10's own deviation. (2) `SELECT ... FOR UPDATE` row lock on the link as the first step, closing a real concurrent-double-submit race the naive approach would reopen (`deal_intake_response`'s `WITH CHECK` requires the link still `pending` at INSERT time, so the response must be written *before* the status flip — which alone isn't race-safe without the lock). Also fixed a real bug found during implementation: `deal_intake_response` needed the same `implicit_returning=False` + client-side `uuid.uuid4` default already applied to `HumanAuditLog` (P3-07) — `dd_public` is INSERT-only there too, and this was the first ticket to ever write to that table. 8/8 new tests + full suite 1044/1044 (fresh volume, independently re-verified by this session, not just the implementer's report) + pyright 0 errors. Implementation doc: `docs/implementations/2026-08-28-p3-11-submit.md`. |
| P3-13 | Vansh | Done | `p3-13-404-audit` (worktree `.claude/worktrees/p3-13`) | merge of `p3-11-submit` (has P3-08/09/10/11) + `origin/staging` (has P3-01/07/12/14 + PR#149) | **Yes — PR pending open** | Real Postgres (dev, port 5434), fresh volume | Pure audit, no code changes to `app/` — traced every public route's failure path against the actual RLS keyhole policies (not just code comments) and confirmed the 404-only contract already held everywhere P3-07/08/09/10/11 built it. New `tests/test_public_404_contract.py` (21 tests) is the empirical proof: byte-identical-body (not just status-code) assertions across all 5 session-authenticated routes x 4 failure modes, plus one combined mutual-identity assertion. One deliberate, documented exception found and left as-is (not a violation): `/uploads/{id}/complete`'s distinct "object not found" 404 and `/presigned-url`'s distinct 409s are post-authentication business outcomes, not link-identity oracles — reasoned through explicitly in the implementation doc, not silently assumed. P3-15 (Suraj's, never built) was NOT included in the merge base despite being named in the ticket's own "branch off" instruction — the actual dependency graph (section 0.3) never lists it as a P3-13 dependency, and it's an unrelated hardening ticket (signed content-length ceiling on `presign_put`) with no bearing on the 404 contract. 21/21 new tests + full suite 1107/1107 (fresh volume) + pyright 0 errors. Implementation doc: `docs/implementations/2026-08-28-p3-13-404-audit.md`. |

## Cross-owner handoff log (who pulled what, when)

- 2026-08-27: Vansh built `p3-base` (staging + P2-03) locally, not yet pushed (it's a local
  integration branch, not itself a PR target — actual ticket branches below are what get pushed).
