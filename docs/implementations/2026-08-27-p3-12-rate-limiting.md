# P3-12 — rate limiting on the public intake surface

**Status:** done, verified against real Postgres + real Valkey, full suite
green (modulo pre-existing unrelated failures, see below).
**Ticket:** P3-12 (`docs/plans/external-deal-intake-link-p3-status.md`,
`docs/plans/external-deal-intake-link-phase-3.md`).
**Session:** 2026-08-27.

---

## What this is

Two independent layers of abuse resistance on the unauthenticated public
intake surface (`/api/public/*`), on top of what P3-07 already built
(per-attempt `failed_attempts` bump + audit row on email mismatch, with a
byte-identical 404 across every failure mode so the route can never be used
as an oracle):

1. **Per-link lockout** (`app/api/public_intake.py`) — once a link's
   `failed_attempts` reaches 5, every further attempt against that link
   404s unconditionally, even one with the correct email, and does so
   *silently*: no further `failed_attempts` bump, no further audit row. The
   5th real mismatch already produced the audit trail; writing on every
   subsequent hammering attempt would flood the audit log P3-13 later
   reviews.
2. **IP-based throttling** (`app/core/rate_limit_middleware.py`) — a
   Valkey-backed fixed-window counter (5 requests / 10 seconds per IP)
   applied to the entire `/api/public/*` prefix, independent of which link
   or route is being hit. This is defense-in-depth, not the hard security
   boundary — a Valkey outage fails **open** (allows the request) rather
   than taking the whole public intake surface down.

Session-authenticated per-`link_id` throttling (mentioned in the original
ticket text) is explicitly out of scope: no route in this codebase yet uses
`get_public_session_db` (P3-08 through P3-11 don't exist here yet), so
there's nothing to throttle by link_id and nothing to test it against.

## What was built

- `app/api/public_intake.py` — `_LOCKOUT_THRESHOLD = 5` module constant; an
  unconditional `if link.failed_attempts >= _LOCKOUT_THRESHOLD: return
  JSONResponse(404, {"detail": "Not found"})` immediately after
  `session, link = session_and_link`, before any email comparison. Reuses
  the exact 404 literal already used for the email-mismatch case a few
  lines down rather than factoring out a shared constant for two call
  sites.
- `app/core/rate_limit_middleware.py` (new) —
  - `check_rate_limit(redis, key, limit, window_seconds) -> bool`: fixed
    window via `SET NX EX` (creates the key with a TTL atomically) then
    `INCR` on subsequent calls, rather than bare `INCR` + a separate
    `EXPIRE` — the two-step form has a window where a crash between the two
    calls leaves a permanent, never-expiring key.
  - `_client_ip(request) -> str`: trusts the **last** entry of a
    comma-separated `X-Forwarded-For` header when present and non-empty
    after stripping; falls back to `request.client.host` (or `"unknown"`)
    otherwise.
  - `RateLimitMiddleware(BaseHTTPMiddleware)`: scoped to
    `path == "/api/public" or path.startswith("/api/public/")`; key
    `f"ratelimit:ip:{ip}"`; `IP_LIMIT = 5`, `IP_WINDOW_SECONDS = 10`; gets
    its Redis client via `app.jobs.queue.get_queue().redis` (no new
    connection pool, no new dependency); on `redis.exceptions.RedisError`
    logs a warning and calls `call_next` (fails open); on limit exceeded
    returns `429` with `{"detail": "Too Many Requests"}` and a
    `Retry-After` header.
- `app/main.py` — `app.add_middleware(RateLimitMiddleware)` registered
  **before** `app.add_middleware(CORSMiddleware, ...)`, with an inline
  comment explaining why (see below — this is load-bearing, not stylistic).
- `tests/conftest.py` — `clear_rate_limit_keys`, a non-autouse fixture that
  `scan_iter`s and deletes every `ratelimit:*` key in Valkey both before
  and after the test it's used in. Also closes the shared `get_queue().redis`
  client at the very end (`await redis.aclose()`) — see the event-loop note
  below, this turned out to be load-bearing, not just tidiness.
- `tests/test_public_intake_session.py` — a local, file-scoped
  `autouse=True` fixture depending on `clear_rate_limit_keys`, required
  (not optional) because `httpx.ASGITransport` gives every test in this
  file the same synthetic client address, so without clearing between
  tests the cumulative request count across the file's existing tests would
  spuriously trip the new 429. Added
  `test_lockout_after_threshold_404s_even_correct_email`: 5 wrong-email
  attempts, then clears the IP-throttle keys (see below), then a 6th attempt
  with the *correct* email, asserting the response is still the byte-
  identical `{"detail": "Not found"}` 404 and `failed_attempts` didn't move
  past 5.
- `tests/test_rate_limit_middleware.py` (new) — monkeypatches
  `IP_LIMIT`/`IP_WINDOW_SECONDS` to small values for test speed; covers (a)
  the `(limit+1)`th request getting 429 with `Retry-After`, (b) two IPs
  (via distinct `X-Forwarded-For` last-entries) tracked independently, (c)
  the last-entry-of-XFF trust behavior explicitly in both directions (same
  last entry / different spoofed first entry still shares a bucket; same
  first entry / different last entry does not).

## Two things worth documenting for whoever touches this next

### Why `RateLimitMiddleware` must be registered before `CORSMiddleware`

Starlette's `add_middleware` inserts each new middleware at position 0 of
its internal list, and the ASGI stack is built by wrapping in `reversed()`
order — so the **last-registered** middleware ends up **outermost** (sees
the request first, the response last). If `RateLimitMiddleware` were
registered after `CORSMiddleware` in `app/main.py`, it would become
outermost, and a 429 short-circuit from the limiter would never pass back
through `CORSMiddleware` on its way out: the browser would see an opaque
CORS failure instead of a readable 429 response. Registering
`RateLimitMiddleware` *before* `CORSMiddleware` keeps CORS outermost, so
429s still carry `Access-Control-Allow-Origin`, and CORS preflight
`OPTIONS` requests get fully handled by `CORSMiddleware` before ever
reaching the limiter (so preflights are never wastefully counted against
the rate limit). `app/main.py` has an inline comment calling this out so a
future edit doesn't "simplify" the ordering back.

### Why `_client_ip` trusts only the *last* `X-Forwarded-For` entry

In prod/staging, only Caddy can reach the app container — `app`, `pgbouncer`,
and `worker` publish no ports in `docker-compose.prod.yml`. Caddy's default
`reverse_proxy` directive *appends* the real peer IP to `X-Forwarded-For`
rather than trusting whatever the client sent, so the last entry is always
the one Caddy itself added; every earlier entry could be attacker-spoofed
and must never be trusted for rate-limit keying. Local dev/tests have no
Caddy in front, so the `request.client.host` fallback (or `"unknown"` if
even that's absent) covers that case, and also covers a malformed/empty
last segment (e.g. a trailing comma).

## A test-environment finding worth flagging: `get_queue()` across pytest-asyncio's per-test event loops

`app/jobs/queue.py::get_queue()` is a process-wide `@lru_cache` singleton —
correct for production (one event loop, one process lifetime), but every
prior test in this repo that touches `get_queue()` monkeypatches it with a
fake (`test_uploads_api.py`, `test_start_deal_analysis_job.py`, etc.) —
nothing before this ticket called the *real* `get_queue().redis` against
live Valkey from more than one test function. Doing so here (per the plan's
explicit instruction to reuse `get_queue().redis` exactly, no new pool)
surfaced a real interaction: `pytest-asyncio` (`asyncio_mode = "auto"`)
gives each test function its own event loop by default, but `redis-asyncio`'s
open connections are bound to the loop that created them. Reusing the
cached client across test functions crashed the *second* test to touch it
with `RuntimeError: Future ... attached to a different loop`.

Fix: `clear_rate_limit_keys`'s teardown calls `await redis.aclose()` after
its final key-clear. This doesn't evict the cached `Queue`/`Redis` client
object (still one process-wide singleton, as designed) — it just closes its
currently-open connections, and `redis-py` reconnects lazily on the next
command issued, in whatever event loop happens to be running then. Verified
manually (two sequential `asyncio.run()` calls, each opening then closing
the same cached client, both succeed) before relying on it across the full
suite.

## Verification

- `uv run pytest` (full suite, real Postgres + real Valkey via
  `docker compose -f docker-compose.dev.yml up -d postgres valkey`) —
  **721 passed**, 5 failed, 9 errors. Confirmed via `git stash` that the
  identical failure/error set exists on the base branch before any P3-12
  change — all are pre-existing and environmental to this local dev DB, not
  caused by this ticket:
  - `tests/test_chunks_rls.py` (3 failed + 5 errors) and
    `tests/test_e2e_pipeline.py` (4 errors) — this dev Postgres instance is
    missing the `chunks` table (`UndefinedTable: relation "chunks" does not
    exist`), unrelated to intake/rate-limiting.
  - `tests/test_l2_retrieval_eval.py::test_retrieval_meets_the_no_regression_floor`
    — same underlying cause.
  - `tests/test_human_audit_log_immutability.py::test_dd_app_can_insert_and_select_human_audit_log`
    — leftover audit rows from repeated manual test runs against this
    persistent local dev DB under the shared test org (audit rows are
    correctly undeletable by `dd_app`, by design) — same finding P3-07's
    own implementation doc already recorded.
  - `tests/test_public_intake_pool.py` — the plan's documented known
    failure did **not** reproduce in this run (all 4 pass); left untouched
    either way per the plan's instruction.
  - `tests/test_public_intake_session.py` (10 tests, including the new
    lockout test) and `tests/test_rate_limit_middleware.py` (3 tests) — all
    pass.
- `uv run pyright` — 0 errors, 0 warnings, 0 informations.

## Deviations from the plan

- **Typing-only, not behavioral.** `get_queue()` is statically typed to
  return the abstract `saq.Queue` (it dispatches on URL scheme at runtime),
  which has no `.redis` attribute — only the `RedisQueue` subclass does.
  Since `VALKEY_URL` is always `redis://` in this app, `get_queue()` is
  always a `RedisQueue` at runtime, but pyright can't know that. Added
  `cast(RedisQueue, get_queue()).redis` at the three call sites (the
  middleware, `tests/conftest.py`, `tests/test_public_intake_session.py`)
  rather than a bare `# type: ignore`, to keep the runtime guarantee
  documented at the point of use. Not a plan deviation in substance — the
  plan's instruction ("get the Redis client via
  `app.jobs.queue.get_queue().redis`") is followed exactly; this is purely
  what was needed to make `uv run pyright` pass at 0 errors.
- **Test-only addition, not in the plan's text.** The event-loop-closing
  fix in `tests/conftest.py::clear_rate_limit_keys` (see the finding above)
  wasn't spelled out in the plan, since the plan didn't anticipate the
  per-test-event-loop interaction. Flagging it explicitly here rather than
  silently folding it in, since it's the one place this implementation
  added logic beyond what was literally specified.
