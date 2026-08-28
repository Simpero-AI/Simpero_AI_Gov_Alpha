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
    stopping the job after 524/~780 passed. **Not fixed** — needs a `valkey` service block
    added to the `test` job, mirroring the existing `postgres` block (image `valkey/valkey:8`,
    `6379:6379`, health-cmd `valkey-cli ping`). Currently blocking PR #137 from going green.
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
    P1-07's design was meant to prevent. **Not fixed** — unclear whether this was a deliberate
    "ship the crash-fix first, adopt the full pool design later" call by Kuntal or an
    oversight; confirm intent with him/Vansh before changing `docker-compose.prod.yml`.
  - **Original DO `max_connections` confirmation (P1-07) — still open, more load-bearing
    now**: flagged since P1 (`docs/plans/external-deal-intake-link-status.md`: "P1-07's
    DigitalOcean `max_connections` confirmation is still open -- this needs Vansh to check the
    actual DO cluster capacity in the console") and never closed out. More load-bearing now
    that the pool-split design above still isn't fully deployed — worth checking the DO
    console capacity and deciding on the `docker-compose.prod.yml` fix together, not
    separately.

## Tickets

| Ticket | Owner | Status | Branch | Based on | Pushed? | Tested against | Notes |
|---|---|---|---|---|---|---|---|
| P3-01 | Vansh | Done | `p3-01-intake-link-generate` | staging | **Yes — [PR #136](https://github.com/Simpero-AI/Simpero_AI_Gov_Alpha/pull/136)** | Real Postgres (dev, port 5434) | Shared effective-status helper: `app/services/intake_links.py::compute_intake_link_effective_status(link) -> str` — P3-02/06/14 import this. 20/20 new tests + full suite (789/789) + pyright 0 errors. |
| P3-05 | Suraj | | | p3-base | | | |
| P3-07 | Vansh | Done | `p3-07-intake-session-endpoint` (worktree `.claude/worktrees/p3-07`) | staging | **Yes — [PR #137](https://github.com/Simpero-AI/Simpero_AI_Gov_Alpha/pull/137)** | Real Postgres (dev, port 5434), fresh volume | Commits `c3af65b` (impl) + `ace8c6f` (test fix). `human_audit_log` insert from a `dd_public` session needed `implicit_returning=False` + a client-side `default=uuid.uuid4` on `HumanAuditLog.id` (see Flagged). Full suite 773/774 (1 pre-existing unrelated failure), pyright 0 errors. **Self-review fix (2026-08-28, commit `b12e908`)**: email comparison switched to `hmac.compare_digest` (constant-time) — see Flagged. |
| P3-10 | Suraj | | | p3-base | | | |
| P3-02 | Suraj | | | P3-01 branch | | | |
| P3-03 | Suraj | | | P3-01 branch | | | |
| P3-06 | Suraj | | | P3-01 branch | | | |
| P3-14 | Vansh | Done | `p3-14-analysis-gate` (worktree `.claude/worktrees/p3-14`) | P3-01's PR branch | **Yes — [PR #138](https://github.com/Simpero-AI/Simpero_AI_Gov_Alpha/pull/138)**, stacked on #136 | Real Postgres (dev, port 5434) | New `IntakeLinkRepo.get_pending_for_deal_unlocked` (deliberately unlocked — reusing the locked `get_pending_for_deal` would serialize `start_analysis` against concurrent link generate/submit). Guard clause in `start_analysis` right after the existing `active_for_deal` 409. 46/46 tests (5 new + regression) + full suite 794/794 + pyright 0 errors. **CI never triggers on this PR** — base is `p3-01-intake-link-generate`, not `main`/`staging` (see Flagged); empty retrigger commit `35d546f` confirmed zero workflow runs. |
| P3-08 | Suraj | | | P3-07 branch | | | |
| P3-12 | Vansh | Done | `p3-12-rate-limiting` (worktree `.claude/worktrees/p3-12`) | P3-07's PR branch | **Yes — [PR #139](https://github.com/Simpero-AI/Simpero_AI_Gov_Alpha/pull/139)**, stacked on #137 | Real Postgres + Valkey (dev, port 5434/6381), fresh volume | Commit `565d737`. Part A: `failed_attempts >= 5` lockout in `public_intake.py`, same 404 body. Part B: new `app/core/rate_limit_middleware.py`, Valkey-backed IP throttle (5 req/10s, `SET NX EX` + `INCR`, keyed `ratelimit:ip:{ip}`, reuses `get_queue().redis` — no new dependency), registered before `CORSMiddleware` (traced Starlette's middleware stack build to confirm ordering keeps CORS outermost). Trusts the last `X-Forwarded-For` entry (Caddy appends, doesn't trust client-supplied header; app container has no published ports per `docker-compose.prod.yml`). Fails open on Valkey errors. 429 (not 404) for throttling — distinct signal from the 404-only contract. Per-link_id throttling deferred (see Flagged — no session-authenticated route exists yet). 9/9 new tests + full suite 778/778 + pyright 0 errors. Implementation doc: `docs/implementations/2026-08-27-p3-12-rate-limiting.md`. **Self-review fixes (2026-08-28)**: merged forward Fix 1 from #137 (`73ce4a0`, not rebased — PR already open); added fail-open regression test `test_valkey_error_fails_open_not_429_not_500` (commit `edb37d0`). Full suite re-verified on a fresh DB volume: 780/780, pyright 0 errors. **CI never triggers on this PR** — base is `p3-07-intake-session-endpoint`, not `main`/`staging` (see Flagged); confirmed after pushing `edb37d0`, zero workflow runs. |
| P3-15 | Suraj | | | P3-10 branch | | | |
| P3-09 | Suraj | | | P3-08 branch | | | |
| P3-11 | Vansh | | | P3-09 + P3-10 branches | | | |
| P3-13 | Vansh | | | P3-07/08/09/10/11/15 (all) | | | |

## Cross-owner handoff log (who pulled what, when)

- 2026-08-27: Vansh built `p3-base` (staging + P2-03) locally, not yet pushed (it's a local
  integration branch, not itself a PR target — actual ticket branches below are what get pushed).
