# Task List

This document is the **source of truth** for all assigned tasks. Original task
descriptions are preserved exactly as given and are never rewritten or removed
once recorded.

---

## SIM-414

**Status:** In Progress (implemented and verified locally; not yet committed/pushed/PR'd)

**Original task description (verbatim):**

> Problem
>
> The geography and sector screening rules can't fire because the firm's saved mandate never reaches the screener.
>
> The Mandate Builder writes the firm's selections to the mandates table (PUT /api/mandate), but screen_deal reads approved_geographies / approved_sectors from investment_profiles.mandate (app/services/screening/workspace_config.py) — a different table that has no writer. So gs_07 (HQ in approved geography) and gs_08 (operates in approved sector) resolve to unknown on every deal, regardless of what the firm selected. This is the "only deals from Canada" case not working end to end.
>
> Independent of SIM-412 / 413. Those promote claims for the claim-driven rules (revenue, concentration); gs_07/gs_08 read a deal field + the mandate config, not claims — so this can land in parallel and lights up the geography/sector questions on its own.
>
> Options
> (a) Point load_workspace_config at the mandates table and transform its stored shape — [{category, category_id, options:[{option, option_id, sub_options?}]}] plus a check-size row — into approved_geographies / approved_sectors. No migration.
> (b) Build the deferred investment_profiles upsert (the "Phase 2" that InvestmentProfileRepo.create notes) that maps a saved mandate into investment_profiles.mandate, keeping the engine's current read path.
>
> Prefer (a): one read-path change + a transformer, with the single source of truth being the mandates row the user actually edits. The Geographies category carries sub_options (e.g. Canada > BC) — decide the containment rule in the transformer (does approving "Canada" approve a deal whose hq_geography is "Ontario"?).
>
> Data dependency / gotchas
> gs_07/gs_08 also need deal.hq_geography / deal.sector populated per deal (manual deal-form fields today) — otherwise they stay unknown for a different reason (SIM-402 territory).
> Matching is exact + case-sensitive in deterministic.py: "Canada" must equal the stored approved value. Normalize both sides in the transformer, or have the taxonomy and the deal form share a vocabulary. (Note the existing Al/ML typo in the seeded taxonomy.)
>
> Acceptance criteria
> A firm whose mandate includes "Canada" screens a deal with hq_geography = "Canada" as gs_07 = Y (pass); a deal outside the approved set as gs_07 = N; and gs_07 = unknown only when the mandate is genuinely unset — verified end to end from PUT /api/mandate to the persisted screening_result.
> gs_08 (sector) behaves equivalently.

**Note:** No Linear ticket exists for this task — the Linear workspace is on the
free plan and has exceeded its issue limit (creation attempt returned
`invalid_request: exceeded the free issue limit`). "SIM-414" is a local label
only, following the numbering of the existing SIM-412/413 branches in this
repo. Create the real Linear issue once the workspace is upgraded or quota is
freed, and update this entry with the resulting Linear ID.

**Tracker:** [docs/track/SIM-414.md](track/SIM-414.md)
