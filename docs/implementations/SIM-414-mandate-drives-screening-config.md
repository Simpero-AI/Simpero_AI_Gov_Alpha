# SIM-414 — The saved mandate drives gs_07/gs_08

**Status:** Implemented, type-checked, and verified end to end against the local
Docker dev stack (real Postgres, real migrations, real HTTP handler, real
screening job). No migration; no shared-database change of any kind.

## The bug

Two tables both held something called a "mandate":

- `mandates` — written by the Mandate Builder through `PUT /api/mandate`. A JSONB
  **list** of the firm's category/option picks. The thing the user actually edits.
- `investment_profiles.mandate` — a JSONB **dict** that
  `app/services/screening/workspace_config.py` read `approved_geographies` /
  `approved_sectors` out of.

Nothing in the application has ever written `investment_profiles.mandate`
(`InvestmentProfileRepo.create` is a plain insert whose upsert was deferred to a
"Phase 2" that never landed; `GET /api/investment-profile` is read-only). So
`load_workspace_config` returned `(None, None)` for every org, and gs_07 ("HQ in
approved geography") and gs_08 ("operates in approved sector") short-circuited to
`unknown` on every deal, forever, no matter what the firm had selected. "We only
look at deals from Canada" could not work end to end.

## The fix

`load_workspace_config` now reads the `mandates` row and transforms it. One source
of truth: the row the user edits. The deferred `investment_profiles` upsert was
deliberately **not** built — a second writer would have recreated the same class of
bug, and a fallback read would have been dead code in an audit-relevant path.
`investment_profiles` is untouched and `GET /api/investment-profile` still serves
it for its other fields.

## Decisions worth knowing

### Categories are matched by stable identity, not display name

Precedence is `category_id` → `slug` → the `category` display string, mirroring
`_mandate_item_key` in `app/api/mandates.py`. The display name is admin-editable,
so it is the one key that silently stops matching after a rename.

This mattered concretely: the canonical sector category is named **"Target
Sectors"** (slug `target_sectors`), not "Sectors". Matching on the display name
alone would have missed it.

An entry whose `category_id` resolves to a *different* category is a definite
non-match, not a reason to keep guessing by name. Display-name aliases are only
consulted for slug-less rows and legacy entries.

`workspace_config` copies the two slug strings rather than importing
`MandateCategorySlug` — that enum is an admin-portal schema, and admin and product
code do not share modules (CLAUDE.md). `test_slugs_match_the_backend_owned_enum`
is the drift guard that makes the copy safe.

### Sub-option containment: a bare parent means "all of it"

`sub_options` is omitted from the saved blob when empty, so:

- **Canada, no `sub_options`** → approves Canada *and its whole subtree* from the
  taxonomy (Ontario, BC, ...). A deal recorded at province level still passes a
  country-level mandate.
- **Canada with `sub_options: [British Columbia]`** → approves Canada + BC only.
  Ontario fails.
- `sub_options: []` reads as omitted, because a saved blob cannot express the
  difference.

Expansion is the full subtree, not one level: the depth-1 cutoff is a Builder UI
limitation, not a policy one, and "Canada approves BC but not Vancouver" would be
arbitrary. The recursion carries a `seen` guard — `parent_option_id` is a self-FK
that permits a cycle, and an infinite loop inside a background job is a far worse
failure than a skipped node.

### Matching folds, but does not alias

Both sides go through `normalize_label`: NFKC → collapse whitespace → casefold.
So `"  canada "` matches `"Canada"` and `"saas"` matches `"SaaS"`.

There is deliberately **no synonym table**: `"US"` is not `"United States"`, and a
deal typed that way reads as a policy `N`. Aliasing would hardcode a vocabulary in
application code that the deal form and the admin-managed taxonomy don't share,
and it would go stale the moment an admin renames an option. The durable fix is a
shared vocabulary between the deal form and the taxonomy (SIM-402 territory).

The `RuleResult` evidence keeps the **raw** deal string, never the folded key —
the audit trail records what the deal actually says.
`test_gs_07_matches_case_and_whitespace_insensitively` pins that.

### Three-state semantics are now per-category

`None` (⇒ `unknown`) versus `[]` versus a populated list was already load-bearing.
The discriminator changed from "does the org have a row" to "did this category
yield any usable option":

| Blob state | result | gs_07 |
| --- | --- | --- |
| no `mandates` row / `mandate` NULL / `[]` | `None` | `unknown` |
| row exists, no geography entry | `None` | `unknown` |
| geography entry, no `options` key or non-list | `None` | `unknown` |
| geography entry with `options: []` | `None` | `unknown` |
| geography entry, options present but none parseable | `None` | `unknown` |
| geography entry with usable options | the list | `Y`/`N` |

A firm that filled in sectors and never opened geographies has genuinely not set a
geography policy; resolving that to `N` would auto-fail every deal against a policy
nobody wrote. An empty-but-present category reads the same way, because in this
blob it is indistinguishable from "the Builder rendered the category and nothing
was ticked". And "options present, none parseable" stays `unknown` rather than `N`
so the audit trail never asserts "we checked; this HQ is not approved" on the basis
of data we could not read.

### Defensive parsing throughout

The blob is unvalidated `list[Any]` on the way in (`UpsertMandateRequest.mandate`),
and this code runs inside `start_deal_screening`, whose exception path fails the
whole `analysis_run`. Every malformed shape — non-dict entry, missing/non-string
`category`, non-list `options`, unusable option item, non-UUID `option_id`,
non-list `sub_options` — is skipped with a warning. Nothing raises. An option that
is no longer in the taxonomy is still approved literally; it just can't be expanded.

### Not cached

gs_07 and gs_08 each call `load_workspace_config` independently: 2 loads per run,
1 SELECT each when the org has no mandate (early return), 3 each when it does.
Deliberately uncached — this is per-org data on an RLS-scoped session, so a
process-level cache is a cross-tenant leak, and a session-keyed one would rest on
"one session, one org", which is a convention here (see
`admin_dependencies._set_org_scope`), not an invariant. Eager loading in
`screen_deal` would also change all eight evaluator signatures and slow the
auto-decline path, since deal-breakers short-circuit before gs_07/gs_08 run.

## Bug found along the way (fixed here)

`MandateRepo.upsert` returned the identity-mapped `Mandate` instance instead of the
`RETURNING` row. `PUT /api/mandate` always loads the previous row first (for the
audit diff), so on every *update* the response echoed the **old** mandate back to
the caller — the frontend re-rendered the pre-save selections. The row on disk was
always correct; only the response was stale. Fixed with
`execution_options={"populate_existing": True}`. Caught by the new
`test_put_mandate_replaces_rather_than_merging`, which is one of the first tests
`PUT /api/mandate` has ever had.

## Tests

- `tests/test_workspace_config.py` — rewritten. Mostly DB-free: the transform is a
  pure function, so the containment rules, category-matching precedence and the
  whole malformed-input matrix run without Postgres. Loader tests seed a real
  taxonomy.
- `tests/test_screening_evaluators.py` — gs_07/gs_08 cases reseeded through
  `mandates`; added the missing gs_08 `N` case, the case-fold cases, the
  no-aliasing case, evidence fidelity, and per-category independence.
- `tests/test_mandate_endpoints.py` — **new.** The write side had zero coverage;
  round-trip, replace-not-merge, audit row, empty diff, check-size shape, RLS.
- `tests/test_screening_reads_the_mandate.py` — **new.** The acceptance test:
  real `PUT /api/mandate` → real screening job → assertions against the persisted
  `screening_result.rule_results`. Nothing in between is mocked, deliberately — a
  test that stopped at `load_workspace_config` would not have caught the original
  bug, because `load_workspace_config` worked fine against the wrong table.
- `tests/conftest.py` — added `user_a_id`; `mandates.user_id` is a NOT NULL FK, so
  any test seeding a mandate needs a real `users` row (and teardown must delete
  `mandates` before `users`).

## Still open

- **`deal.hq_geography` / `deal.sector` are free-text and manually entered.** With
  the mandate wired up, a deal missing those fields still reads `unknown` — now for
  the other reason. The two `reason` strings distinguish the cases.
- **No shared vocabulary** between the deal form and the taxonomy. Until there is
  one, a mismatch reads as a clean policy `N`, indistinguishable from a real
  rejection.
- **Renaming a taxonomy option orphans saved mandates** — the blob stores the
  display string, so a rename silently stops matching. Pre-existing; the option_id
  path mitigates it for expansion but not for the literal label.
- **The "Al/ML" typo is live data, not code.** There is no taxonomy seed in the
  repo; categories and options are created by hand through the admin portal. The
  fix is `PATCH /api/admin/mandates/options/{id}`. Case-folding does not help —
  `Al` and `AI` are different letters.
- **`_diff_mandate` reads `o["option"]` unguarded** (`app/api/mandates.py`). A
  previously-saved entry missing that key would 500 the next `PUT`. Unreachable
  from our own frontend, out of scope here, and deliberately not "fixed" alongside
  the read path — the transformer's tolerance must not be read as implying the
  write path is tolerant.
