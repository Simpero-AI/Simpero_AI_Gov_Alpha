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

## Tickets

| Ticket | Owner | Status | Branch | Based on | Pushed? | Tested against | Notes |
|---|---|---|---|---|---|---|---|
| P3-01 | Vansh | Done | `p3-01-intake-link-generate` | p3-base | No | Real Postgres (dev, port 5434) | Shared effective-status helper: `app/services/intake_links.py::compute_intake_link_effective_status(link) -> str` — P3-02/06/14 import this. 20/20 new tests + full suite (742/743, 1 pre-existing unrelated failure) + pyright 0 errors. |
| P3-05 | Suraj | | | p3-base | | | |
| P3-07 | Vansh | | | p3-base | | | |
| P3-10 | Suraj | | | p3-base | | | |
| P3-02 | Suraj | | | P3-01 branch | | | |
| P3-03 | Suraj | | | P3-01 branch | | | |
| P3-06 | Suraj | | | P3-01 branch | | | |
| P3-14 | Vansh | | | P3-01 branch | | | |
| P3-08 | Suraj | | | P3-07 branch | | | |
| P3-12 | Vansh | | | P3-07 branch | | | |
| P3-15 | Suraj | | | P3-10 branch | | | |
| P3-09 | Suraj | | | P3-08 branch | | | |
| P3-11 | Vansh | | | P3-09 + P3-10 branches | | | |
| P3-13 | Vansh | | | P3-07/08/09/10/11/15 (all) | | | |

## Cross-owner handoff log (who pulled what, when)

- 2026-08-27: Vansh built `p3-base` (staging + P2-03) locally, not yet pushed (it's a local
  integration branch, not itself a PR target — actual ticket branches below are what get pushed).
