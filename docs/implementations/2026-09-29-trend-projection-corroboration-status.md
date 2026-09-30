# Per-year corroboration status on the trend & projections

2026-09-29 (PR #248). No `docs/plans/` doc preceded this — the work started from
a QA ask ("corroboration needs to run on the financial projections / 3-year
trend") and an end-to-end investigation; this implementation doc stands in for
both. Frontend counterpart: `Simpero_AI_Gov_Web` PR #79.

## Problem

The Financials tab's **3-Year Trend** and **Projections** grids rendered bare
numbers. A prior-year figure the corroboration engine had already confirmed
against SEC EDGAR looked no different from an un-checked one, so the request read
as "corroboration doesn't run on the trend/projections."

The investigation showed the ask was a **display gap, not an engine gap**:

- `start_deal_corroboration` loads *every* corroboratable claim with **no period
  filter**; `span_promotion.promote_exact_span` promotes each located prior-year
  claim to `cited` individually (no per-attribute collapse); and
  `corroboration_sources/sec_edgar.py`'s `check` keys on each claim's own
  `period_year`, with `_lookup_annual_fact` restricting to the datapoint whose
  own period covers that year (comparatives excluded). So the engine already
  corroborates each year independently.
- Verified live over a real NVIDIA 10-K claims export: `revenue`, `gross_profit`,
  `ebit`, `net_income`, `operating_cash_flow` and the balance-sheet concepts all
  return per-year AGREE for FY2024 / FY2025 / FY2026. `_CONCEPTS` already maps the
  P&L-trend concepts (an earlier EDGAR-coverage PR); `ebitda` and the margins stay
  intentionally unmapped (no clean GAAP tag — deriving them would fabricate).

The gap was purely that the two views threw the verdict away: `FinancialFact`
(statement rows) carried `status`/`citation`, but `TrendPoint` carried only
`period`/`value`/`year` and `ProjectionRow` only a `list[str | None]` of values.

## Decision

Thread each figure's provenance through both views, mirroring `FinancialFact`,
rather than change anything in the engine (which was already correct):

- `TrendPoint` gains `status` / `citation` / `source_url` /
  `reconciliation_mismatch`, populated from the point's claim. `status` is the
  rolled-up trust, which already folds in the per-year corroboration verdict.
- `ProjectionRow` gains `cells: list[ProjectionCell]`, aligned **by index** to
  `values` (`status=None` for an absent cell). Additive — `values` is unchanged —
  so the change is deploy-order-tolerant with the frontend.
- `build_financials_trend` / `build_financials_projections` now take `filenames`
  + `source_urls` (as `build_financials_view` already did) to resolve citations;
  the endpoint passes maps it already builds.

**Forward-figure guarantee, enforced in code** (`_display_status`): an
estimate/projected period (`period_kind` `E`/`P`) can never carry an external
verdict (`verified`/`conflicted`) — no historical registry can corroborate, or
contradict, a forecast. A forward figure that reached `verified` is shown as
`partially_verified` (reaching `verified` always implies a prior internal verify,
so this strips only the external-corroboration layer, never invents trust); a
spurious external conflict on a forecast is dropped the same way. This decouples
the honesty property from EDGAR's data horizon: a near-current estimate whose year
is already filed, or a forecast with a mis-resolved `period_year`, is never badged
externally corroborated. Actuals (`A`/None) pass through unchanged. Added after a
code review flagged the guarantee was inherited from EDGAR's data horizon rather
than enforced.

## What changed

- **`app/services/financials_view.py`**
  - `TrendPoint` gains `status`/`citation`/`source_url`/`reconciliation_mismatch`
    (all defaulted, so older callers/tests still construct it).
  - New `ProjectionCell` (`status`, `citation`, `source_url`,
    `reconciliation_mismatch`); `ProjectionRow` gains `cells`, aligned to `values`.
  - `_display_status(claim)` (+ `_FORWARD_PERIOD_KINDS`, `_EXTERNAL_VERDICT_STATUSES`):
    the forward-figure clamp above; applied to both trend points and projection cells.
  - `build_financials_trend` / `build_financials_projections` take `filenames`
    + `source_urls`. Projection rows build each `(value, cell)` pair in **one**
    per-year pass and split, so a value and its badge can't drift out of alignment.
- **`app/schemas/deals.py`**: `FinancialTrendPointResponse` gains the four fields;
  new `FinancialProjectionCellResponse`; `FinancialProjectionRowResponse` gains
  `cells` (defaults to `[]`).
- **`app/api/deals.py`**: passes `filenames`/`source_urls` to both builders; maps
  `ProjectionCell` → `FinancialProjectionCellResponse`.

## Tests

- `tests/test_financials_view.py`: trend points carry per-year status + citation;
  projection cells carry per-column status + `None` for an absent cell; forward
  projection cell and forward trend point never show an external verdict; a direct
  `_display_status` table (verified/conflicted on `E`/`P` → partial; actual +
  non-external statuses unchanged).
- `tests/test_deal_financials_endpoint.py`: the projections grid serializes `cells`
  aligned to `values` in camelCase; trend points serialize their provenance fields
  in camelCase.
- Whole-repo `pyright` 0 errors; `ruff format`/`ruff check` clean.

## Known limitations / follow-ups (not blocking)

- **`ebitda` and the margins are not EDGAR-corroborated** — no clean single GAAP
  tag; deriving them would fabricate. Left unmapped by design.
- **No re-analysis needed for the display** — it reads existing rolled-up
  `claim.status`. A deal analysed *before* the EDGAR concept-coverage landed shows
  `gross_profit`/`ebit` as `cited` until re-analysed; `revenue`/`net_income` were
  always mapped.
- **Corroboration-status non-determinism (observed).** Across three end-to-end
  runs of the same NVIDIA 10-K, all extracted values were identical, but three
  balance-sheet items (`current_assets`, `current_liabilities`,
  `total_liabilities`) flapped `verified` ↔ `partially_verified` run-to-run — the
  same value, EDGAR-corroborated in one run, no-signal in another. Root cause is
  the best-effort EDGAR HTTP path (a transient fetch/rate-limit miss yields
  no-signal → the roll-up leaves the claim `partially_verified`). Not a
  data-correctness bug; hardening the EDGAR fetch (retry/backoff) so the badge is
  stable across runs is a candidate follow-up.
- Citation-page and prior-year-cell coverage also drift run-to-run (extraction
  non-determinism — thinking forces `temperature=1.0`, the documented parser
  blocker); values do not.
