# SIM-370: within-page CONTRADICTS — keep the parser's emission, fix the doc

**Decision: (a)** — update the design doc's §4 lock to accept within-page
`CONTRADICTS` from the parser's E1 reducer. No parser code change.

**Rationale:**

- The E1 reducer's within-page `contradicts` emission
  (`extract_service.py` building `type="contradicts"`; `emit.py`'s
  `EdgeType = Literal["same_fact", "contradicts"]`) is deliberate, per its
  own docstring: the disagreement between tiers on the same page is signal
  worth keeping, not noise to suppress.
- E3 period resolution (SIM-345) already makes the within-page grouping
  safe — the failure mode the doc's original lock was guarding against
  (grouping claims from different periods as if they contradicted) is
  handled by a control that didn't exist when the lock was written.
- The doc's §4 "confirmed against the code / there is no cross-claim logic"
  note predates the reducer's contradicts pass — it was accurate against an
  earlier commit (`main @ f5d60fe`) and is now stale, not a considered
  restriction the code violates.
- Changing the code instead (option b) would mean retiring working,
  deliberately-written tests and narrowing CONTRADICTS to alpha-only for no
  functional gain — the doc is what's out of date here, not the parser.

**Why this is more than stale — it's internally inconsistent (review feedback,
Kuntal):**

- `same_fact` and `contradicts` are not two independent features — they are
  the two possible OUTCOMES of the same within-page reducer comparing a pair
  of claims (agree vs. disagree), per SIM-341's own landed decision: *"Within
  a page, emit `same_fact` (winner = table, prose kept as corroboration) and
  `contradicts` (values differ — both claims kept, neither dropped)."* A
  doc that blesses within-page `same_fact` as a legitimate local match while
  banning within-page `contradicts` is banning one branch of a single
  `if values_match: ... else: ...` — there is no separate mechanism to
  restrict, only a coin-flip outcome of the one that's already allowed.
- The §4 premise that within-page comparison "needs E2/E3, which don't exist
  at parse time" is not just outdated, it's factually wrong as stated: E2
  (attribute vocabulary mapping, SIM-344) and E3 (period resolution,
  SIM-345) are themselves **parser-side** stages (`parser_service/extract.py`,
  per both tickets' `Files` field), not alpha/backend stages — both are
  `Done` (completed 2026-08-01), and the E1 reducer already keys its
  within-page grouping on their output. The thing the lock said couldn't
  exist at parse time is itself built at parse time.

**Consequence for SIM-371 (3a reconciliation):** the cross-page reconciliation
pass must not double-count a `contradicts` edge the E1 reducer already wrote
within-page for the same claim pair. In the actual implementation
(`app/services/reconciliation.py`) this is enforced structurally, not by an
edge-existence lookup: 3a only ever compares claim pairs on **different**
pages (`if other.page == canonical_claim.page: skip`), so a same-page pair —
which is exactly what E1 already covers — is never even considered a
candidate, let alone re-linked. Tested in
`tests/test_reconciliation.py::test_same_page_pair_is_left_to_the_e1_reducer`.

**Proposed §4 replacement text** (ready to paste into the design doc once
someone with access can reach it):

> §4 (revised): The parser MAY compare claims within a single page at the
> extraction fan-in (the E1 reducer) and emit either `SAME_FACT` (values
> agree; table wins as canonical, prose retained as corroboration) or
> `CONTRADICTS` (values disagree; both claims retained, neither dropped) as
> the outcome of that one comparison. This is safe because period resolution
> (E3) and canonical attribute mapping (E2) run upstream of the reducer, so a
> within-page comparison is never grouping claims that merely look alike but
> describe different periods or attributes. Cross-page and cross-document
> comparison remains alpha's responsibility (Step 3a/3b) and is unaffected by
> this section.

**What this decision does NOT do:**

- **Does not edit the actual design doc.** The design doc ("Plan for
  everything downstream of parsing") is external to both
  `Simpero_AI_Gov_Alpha` and `Simpero_Gov_AI_Services` — no file for it
  exists in either repo, and no Linear document is linked from SIM-368 or
  its children. The replacement text above is drafted and ready; someone
  with access to that doc still needs to paste it into §4 and this record
  should be updated with a link to where it landed.
- **Does not touch parser code.** `extract_service.py` / `emit.py` live in
  the sibling repo `Simpero_Gov_AI_Services`, out of scope for this session
  (see the branch's scope note). Decision (a) requires no parser change
  anyway, so there is nothing pending there beyond the doc update above.
