# Claim status lifecycle — Implementation Summary

**Status:** implemented on two stacked branches, not yet merged.
**Tickets:** SIM-412 (exact-span promoter), SIM-413 (status roll-up wiring).
**Builds on:** SIM-59 (exact-span resolver), SIM-359 (binding auditor),
SIM-254 (`app/services/status_rollup.py`, merged to staging as PR #91),
SIM-252 (`app/services/corroboration.py`).
**Session:** 2026-08-20

---

## What this is

Closing the gap between "the parser extracted a claim" and "the platform is
willing to act on it". Before this, `claims.status` never moved after ingest:
the parser emits every PDF claim at `proposed`, and SIM-368 deliberately built
the verification passes as edge-and-flag only ("not a status-promotion
engine"). Measured consequence on staging — `SELECT status, count(*) FROM
claims` was 100% `proposed`/`missing`, zero `cited`:

- `app/services/screening/claims_lookup.py` trusts `cited`,
  `partially_verified`, `verified` — so **every screening evaluator saw an
  empty claim set**.
- `app/services/corroboration.py::record_corroboration_result` refuses a claim
  below `cited`, so **no external corroborator could run at all** (SIM-262
  entity verification, SIM-408 EDGAR harvest).
- `app/services/status_rollup.py` had shipped with no caller, so trust status
  never flowed even where it could have.

## What was built

### SIM-412 — `app/services/span_promotion.py` (new)

`promote_exact_span(session, *, data_source_id) -> PromotionSummary`, following
the same pass contract as `reconciliation.py`/`consistency.py`: caller owns RLS
scoping and the transaction, mutations happen in place, no flush and no commit,
counters come back in a dataclass.

Promotes a claim only when **all** of:

| Condition | Why |
| --- | --- |
| `status == 'proposed'` | this pass owns exactly one transition, and the filter is also what makes a re-run a no-op |
| `kind IN ('pdf', 'docx')` | XLSX literals are born `cited`/`direct_read`; an exact *text* span cannot vindicate a cell |
| `char_start`/`char_end` set | never promote on a citation that was never resolved |
| no `binding_unsupported` flag | SIM-359 found the span does not support the value — promoting would launder a known-bad citation |

Sets `status='cited'` and `verification_method='exact_span'` **together** —
`ck_claims_checked_requires_method` rejects one without the other.

Wired into `app/jobs/tasks/start_deal_verification.py` as the first call in the
existing per-document loop, and into `scripts/run_verification.py`.

### SIM-413 — the roll-up gets a caller

A deal-scoped loop in `start_deal_verification.py`, after 3a/3b and before the
run is marked successful:

```python
await session.flush()
for claim in (await session.scalars(rollup_stmt)).all():
    rollup_counts[await roll_up_status(session, claim)] += 1
```

`rollup_stmt` filters on `CORROBORATABLE_STATUSES`; that filter is the guard,
not a `try/except` — `roll_up_status` raises `ClaimNotCorroboratableError` on
anything below `cited`, and a real run is full of `missing`/`proposed` claims.
Resulting counts go onto the job comments and into the
`analysis_verification_completed` audit payload as `status_rollup`.

`scripts/run_verification.py` gained the equivalent `_roll_up_all` plus a
before/after claim-status histogram — the observable both tickets are measured
on.

## Decisions made during implementation

- **Order is promote → 3a → 3b → roll up.** Promotion is a *provenance*
  judgment (the span resolved, the auditor did not fault it) and owes nothing
  to the cross-claim passes, so it goes first. The roll-up is the only step
  that *reads* what the others wrote — `formula_mismatch` flags and
  `contradicts` edges are its internal-disagreement signal — so it goes last.
- **The roll-up is deal-scoped, not per-document**, unlike 3a/3b. Trust is a
  property of the claim, and a `contradicts` edge can point at a claim that
  arrived in a different file.
- **`roll_up_status` is called per claim, unbatched.** It issues two SELECTs
  per claim (contradicts-edge lookup, corroboration-events lookup). Kept as-is
  deliberately: smallest diff, SIM-254's 549 lines of tests stay valid. See
  the gap below.
- **Promotion stayed ticket-exact.** Only `binding_unsupported` disqualifies.
  Because `ck_claims_found_requires_span` already guarantees a span on every
  non-`missing`, non-XLSX claim, this promotes nearly every PDF claim.
  `tests/test_span_promotion.py::test_unrelated_flags_do_not_block_promotion`
  pins that on purpose, so tightening it later is a visible change.

## Bugs found and fixed

- **`autoflush=False` made the roll-up a silent no-op.**
  `app/core/database.py` builds sessions with `autoflush=False`, so the
  promoter's in-place `status` mutations were invisible to the roll-up's
  `SELECT ... WHERE status IN (...)`. The first run of the wiring tests matched
  zero claims and still reported success — the worst possible failure shape.
  Fixed with an explicit `await session.flush()` before the roll-up query (and
  before the sandbox script's histogram, which had the same defect and printed
  the pre-promotion numbers). Documented in `span_promotion.py`'s docstring so
  the next pass that reads `status` back does not rediscover it.

## The behaviour change to know about

With `exact_span` counting as a **strong** method and no corroboration events
in the pipeline yet:

| Situation | Ends at | Visible to screening? |
| --- | --- | --- |
| promoted, no contradicts edge, no `formula_mismatch` | `verified` | yes |
| promoted, but 3a/3b flagged it | `inconclusive` | **no** |
| `binding_unsupported` | `proposed` | no |
| `missing` | `missing` | no |

The `inconclusive` demotion is SIM-254's pinned, intended behaviour, but it is
a real product change: a claim screening *could* have read at `cited`
disappears the moment the roll-up runs over it. Pinned by
`tests/test_status_rollup_wiring.py::test_contradicted_claims_are_demoted_out_of_screening_trust`.

## Known, named gaps

- **The binding auditor is opt-in and lives in another repo.** It runs behind
  `--audit` in `Simpero_Gov_AI_Services`. If production extraction does not
  pass it, no claim ever carries `binding_unsupported` and the exclusion is a
  no-op. Worth confirming before treating the flag as real protection.
- **The auditor's mode/evidence is discarded at ingest.** Only the flag type
  survives; `flag_log` is dropped by `start_deal_verification.py`. Fine for
  this pass (any binding fault is disqualifying) but it means the *reason* a
  claim was held is not queryable.
- **Roll-up is 2 SELECTs per claim.** Over a real CIM's hundreds of claims that
  is a few hundred round trips inside one transaction, under a 120s
  `statement_timeout`. Measure on a real run before assuming it is fine;
  batching the contradicts-edge and corroboration-event lookups is the fix.
- **`superseded_by_same_fact` claims are still promoted.** They are excluded
  from screening downstream by `claims_lookup`, so this is harmless today, but
  the status itself says `verified` on a claim an edge-aware reader should
  skip.

## Verification performed

Local Postgres (`docker-compose.dev.yml`, port 5434), freshly migrated.

- `uv run ruff check .` / `ruff format --check .` — clean.
- `uv run pyright` — 0 errors.
- `uv run pytest tests -q` on the SIM-413 branch: **449 passed, 3 failed,
  9 errors**. All 12 failures/errors are `tests/test_chunks_rls.py` and
  `tests/test_e2e_pipeline.py` failing with `relation "chunks" does not
  exist` — a gap in this local DB, **confirmed identical on a pristine
  `origin/staging` checkout** (stash + re-run) and therefore not caused by
  these changes. CI provisions the table and is unaffected.
- New tests: `tests/test_span_promotion.py` (19), the job-level promotion case
  in `tests/test_start_deal_verification_job.py` (1), and
  `tests/test_status_rollup_wiring.py` (5).

**Not yet done:** the end-to-end sandbox run on a real CIM
(`scripts/ingest_claims.py` → `scripts/run_verification.py --commit`) that
would produce the before/after status histogram both tickets name in their
acceptance criteria. That needs the sandbox corpus, which is not in this
environment.

## Out of scope / deliberately not built

- **Reranker / prose promotion.** Needs SIM-250's two-tier enforcement rule,
  which is parked pending the agent-structure decision.
- **Routing `binding_unsupported` claims to human review.** They simply stay
  `proposed`.
- **The per-claim external corroborator queue** (SIM-253). SIM-413 is the
  internal-only first step carved out of it; once SIM-262/SIM-408 write
  corroboration events, the same call also yields `partially_verified` /
  `conflicted` with no change here.
