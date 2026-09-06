"""Ingest the parser's retrieval chunks into the `chunks` table -- the production
half of the chunk seam (SIM-338), promoted out of scripts/ingest_chunks.py so the
deal-flow job (start_deal_verification) and the demo CLI share one row builder.

Two shaping rules the chunks table's columns don't carry directly:
- scale_context (a table's scale banner, e.g. "$ in millions") folds into the
  stored/embedded `content` as a prefix, so the scale is both searchable (the
  Postgres-generated content_tsv) and, later, embedded.
- the per-block `spans` collapse to one covering (char_start, char_end); tables
  and charts carry no span (their citation is a bbox) and come back NULL.

Two differences from the demo, both because production has real rows:
- document_id is the deal's REAL data_source id (the per-document FK the verify
  job already has), NOT the demo's uuid5(sha) stand-in.
- each row gets a DETERMINISTIC id (uuid5 of data_source_id + the chunk's
  document-order), so a re-analysis re-ingesting the same document collides on
  the primary key and ON CONFLICT DO NOTHING makes it idempotent -- the same
  posture claims use with their (org, data_source_id, claim_ref) unique index.

Embeddings are deliberately NOT set here: the verify ingest runs inside a held
DB transaction, and a Voyage call there would pin a pooled backend across a slow
network round-trip (the HTTP-outside-transaction rule). Chunks are written with a
NULL embedding -- immediately usable for sparse (content_tsv) retrieval, which the
model explicitly supports ("a chunk can be written before it's embedded") -- and a
dense-embedding backfill pass is the tracked follow-up.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

# A fixed namespace for the deterministic chunk id. Distinct constant (not
# NAMESPACE_OID, which the demo reused for the sha->document_id stand-in) so the
# two id spaces can never collide.
_CHUNK_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "simpero/chunks/v1")


def fold_content(chunk: Mapping[str, Any]) -> str:
    """The text stored + (later) embedded. scale_context folds in as a prefix so
    the scale rides along in both retrieval legs; the chunks table has no
    scale_context column, so this is where that decision lives."""
    content = chunk["content"]
    scale = chunk.get("scale_context")
    return f"[{scale}] {content}" if scale else content


def _span(chunk: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """One covering (char_start, char_end) from the chunk's per-block spans, or
    (None, None) for a span-less chunk (table/chart -- cited by bbox)."""
    spans = chunk.get("spans") or []
    if not spans:
        return None, None
    return min(s[0] for s in spans), max(s[1] for s in spans)


def chunk_id(data_source_id: uuid.UUID, chunk: Mapping[str, Any]) -> uuid.UUID:
    """Deterministic primary key for idempotent re-ingest: keyed on the document
    (data_source_id, stable across re-analysis) + the chunk's document-order,
    which chunk_document assigns uniquely per document. Re-ingesting the same
    document reproduces the same ids, so ON CONFLICT DO NOTHING is a no-op."""
    return uuid.uuid5(_CHUNK_NAMESPACE, f"{data_source_id}:{chunk['order']}")


def chunk_row_values(
    chunk: Mapping[str, Any], *, org_id: int, data_source_id: uuid.UUID
) -> dict[str, Any]:
    """One seam-JSON chunk -> a Chunk row dict for a bulk pg_insert. embedding /
    embedding_version are left unset (NULL) and content_tsv is Postgres-generated,
    so neither is set here."""
    char_start, char_end = _span(chunk)
    return {
        "id": chunk_id(data_source_id, chunk),
        "org_id": org_id,
        "document_id": data_source_id,
        "content": fold_content(chunk),
        "element_type": chunk.get("element_type"),
        "page": chunk.get("page"),
        "char_start": char_start,
        "char_end": char_end,
    }
