# SIM-369: `edges`/`type` naming — keep as shipped

**Decision:** keep the shipped table/column names (`edges`, `type`) exactly as
SIM-366 (#63) landed them. Do not rename to the design doc's `claim_edges` /
`edge_type` notation.

**Rationale:**

- SIM-369 was scoped as "merging #63 as-is" — the live schema is the starting
  point, not the design doc's notation.
- `edges` is a live RLS table with real FKs (`claims.id`, `ON DELETE
  RESTRICT`), a CHECK constraint, and an existing RLS policy. Renaming the
  table or its `type` column is a heavier migration (rename + update every
  reference: the model, `scripts/ingest_claims.py`, `tests/test_edges_ingest.py`,
  the RLS policy definition) for a naming-only change with no functional
  benefit.
- The doc's `claim_edges`/`edge_type` naming is notation describing intent,
  not a requirement the running system must mirror verbatim.

**Action taken here:** none on the code side (no rename). The design doc
itself is external to both repos and unreachable from this session — the
team should update its `claim_edges`/`edge_type` references to `edges`/`type`
by hand to match what's actually shipped.
