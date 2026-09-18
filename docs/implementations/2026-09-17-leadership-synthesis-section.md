# Leadership synthesis section

2026-09-17. No `docs/plans/` doc preceded this — the architecture was worked
out inline (research → architect plan → three sign-off decisions) rather than
as a written plan doc; this implementation doc stands in for both.

## Problem

The frontend's Founders tab renders per-person cards (name, title, background,
pull-quote) from `ICMemoDeliverable.managementTeam`, a frontend-only field
populated by an IC-memo composer that doesn't run for most deals. No structured
per-person data existed anywhere in this backend to fall back to:

- `GET /deals/{id}/company`'s `relatedParties` (`build_company_view`,
  `app/services/company_view.py`) is claims-only/LLM-free by design; a related-
  party claim's `entity` field is opaque free text with no person/company
  discriminator anywhere in `contracts/claims.schema.json`.
- `GET /deals/{id}/company-synthesis`'s `related_parties` section was AI-
  summarized prose (`{text, citation}` points), not split into name/title/
  background.

Optional/nice-to-have — the frontend already renders Related Parties honestly
as a fallback when this is absent.

## Decision

Added a 7th section (`leadership`) to the existing `field_synthesis` pipeline
and `synthesis_snapshot` persistence, rather than touching `build_company_view`.
`build_company_view`'s claims-only/LLM-free invariant is load-bearing (stated in
its own docstring) and claims carry no person/company typing to build on —
adding that typing belongs upstream in the parser service (Simpero_Gov_AI_Services),
out of scope here. `field_synthesis` already does grounded chunk-retrieval +
LLM summarization for the existing `related_parties` prose section, so a people-
shaped variant of the same mechanism was the natural fit.

Three explicit sign-offs (Vansh, 2026-09-17):

1. **Section/wire key: `leadership`** (not `managementTeam` or `founders`) —
   generic enough to cover a non-founder CEO/director; frontend will need new
   code to consume this regardless of key name.
2. **Model config: reuse `settings.field_synthesis_model`**, no new per-feature
   `Settings` field — this runs inside the same `generate()` call as the other
   six sections with one `model` param already threaded through; a dedicated
   `leadership_synthesis_model` would always equal it.
3. **Surname-presence gate: kept strict.** A silently invented executive is
   worse than a missing card, and the frontend already falls back honestly.
   Accepted false-negative risk: a filing's "J. Smith" vs. a model's "John
   Smith" would drop a real person.

## What changed

- **`app/services/field_synthesis.py`**
  - `SynthPerson` (frozen dataclass: `name`, `title`, `background`, `citations`,
    `chunk_ids`) with `to_json`/`from_json` mirroring `SynthPoint`.
  - `SectionSynthesis` gains `people: list[SynthPerson]` — tolerant
    deserialization means an old persisted snapshot row with no `people` key
    degrades to `people == []`, no migration, no version bump.
  - `SectionSpec` gains `people: bool = False`; new `leadership` spec appended
    to `COMPANY_SECTIONS` with its own retrieval query (unvalidated first
    guess — sparse-only BM25 means the query string *is* retrieval quality;
    worth tuning against a real deal).
  - `_PEOPLE_SYSTEM`/`_PEOPLE_TOOL` (`report_people` tool) — a second prompt/
    tool pair, selected via `spec.people`. `_call_model` now takes explicit
    `system`/`tool`/`max_tokens` (people call: 2048 tokens, up from 1024, to
    avoid truncating 8+ bios).
  - `_verify_people` — same deterministic grounding gate as `_verify_points`
    (drop anyone citing only invented chunk ids), **plus a new surname-
    presence gate**: a person's surname must actually appear in the text of a
    chunk they're grounded in, not just cite a structurally valid id. This is
    the guard against a name confabulated onto a real citation — the existing
    citation gate alone can't catch that.
  - `generate()`'s reason-code classification (`ok`/`model_no_tool_call`/
    `model_no_answer`/`ungrounded`) now reads `people` vs `points` via a
    `result_key` variable so a people-only successful section is correctly
    classified `ok`, not empty.

- **`app/schemas/deals.py`** — `CompanySynthPersonResponse` (`name`, `title`,
  `background`, `citation`); `CompanySynthSectionResponse` gains
  `people: list[CompanySynthPersonResponse] = []`.

- **`app/api/deals.py`** — extracted `_citation_label()` helper (was inline in
  `_synthesis_to_response`) and reused it for both the points and people
  mapping paths. Route handler unchanged.

- **Not touched**: `app/services/company_view.py`, `GET /deals/{id}/company`,
  `app/jobs/tasks/start_deal_synthesis.py`, `app/models/synthesis_snapshot.py`,
  `SynthesisSnapshotRepo`, `app/core/config.py` — no DDL, no migration, no job
  wiring change (the new section rides inside the existing `retrieve()` →
  `generate()` → persist flow for free).

## Tests

`tests/test_field_synthesis.py` — 14 new cases covering every `_verify_people`
gate individually, including the highest-value one: a person with a
structurally valid citation whose surname doesn't appear in the cited chunk's
text is dropped (this is the only coverage of the new gate; the existing
citation-only gate would let it through). Also: a people-only section reaching
`ok`, and round-trip serialization including the old-shape-row-has-no-`people`-
key → `[]` case (the actual "no migration needed" guarantee). All pure/no-DB/
no-network — `_call_model` is monkeypatched, never hits the real API.

`tests/test_company_synthesis_mapping.py` — 3 new cases: people section maps to
wire shape with deduped citation label; unresolvable citations → `citation:
null`; page-less citation → bare filename.

`uv run pyright`: 0 errors. `uv run pytest tests/test_field_synthesis.py
tests/test_company_synthesis_mapping.py -q`: 37 passed. Both files are DB-free;
did not run the full suite (this sandbox has no reachable Postgres for the
DB-backed deal/upload fixtures elsewhere in the suite).

## Known limitations / follow-ups (not blocking)

- Retrieval query wording for the `leadership` section is a first guess, not
  validated against a real deal.
- Sparse-only BM25 retrieval (embeddings not yet backfilled) — a bio page that
  never uses the query's vocabulary won't be retrieved at all; improves for
  free once dense retrieval is backfilled.
- No backfill: existing `synthesis_snapshot` rows never gain a `leadership`
  section until the deal is re-analyzed (consistent with the append-only
  snapshot model — `UPDATE`/`DELETE` already revoked from `dd_app`).
- The durable fix — person/company typing at claim-extraction time in
  Simpero_Gov_AI_Services, making people available on the claims-only
  `build_company_view` path with no LLM involved — is out of scope for this
  repo and wasn't attempted here.
