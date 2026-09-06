"""Unit tests for chunk_ingest -- the pure seam-JSON -> Chunk-row mapping the
verify job uses. No DB. Guards the shaping rules (scale folds into content, spans
collapse to a covering range) and the re-analysis idempotency key."""

import uuid

from app.services.chunk_ingest import chunk_id, chunk_row_values, fold_content


def _chunk(**kw) -> dict:
    base = {
        "content": "Revenue was $15,295 in fiscal 2024.",
        "element_type": "prose",
        "page": 11,
        "order": 3,
        "document_id": "a" * 64,
        "source_file": "cim.pdf",
        "scale_context": None,
        "scale_multiplier": None,
        "spans": [[0, 35]],
        "bbox": None,
        "section": "Financials",
        "flags": [],
    }
    base.update(kw)
    return base


_DS = uuid.uuid4()


def test_fold_content_prefixes_scale_context():
    # A table's scale banner folds into the content so the scale rides both
    # retrieval legs (the chunks table has no scale_context column).
    chunk = _chunk(content="Revenue 15,295", scale_context="$ in millions", element_type="table")
    assert fold_content(chunk) == "[$ in millions] Revenue 15,295"


def test_fold_content_without_scale_is_unchanged():
    assert fold_content(_chunk(content="plain prose", scale_context=None)) == "plain prose"


def test_row_values_covers_the_span_range_and_maps_the_document():
    chunk = _chunk(spans=[[10, 40], [5, 22]], page=7)
    row = chunk_row_values(chunk, org_id=1, data_source_id=_DS)

    assert row["document_id"] == _DS  # the REAL data_source id, not the sha stand-in
    assert row["org_id"] == 1
    assert row["char_start"] == 5  # min start
    assert row["char_end"] == 40  # max end
    assert row["page"] == 7
    assert row["element_type"] == "prose"


def test_row_values_no_span_is_null_char_range():
    # A table/chart is cited by bbox, not a prose span -> NULL char range.
    row = chunk_row_values(_chunk(element_type="chart", spans=[]), org_id=1, data_source_id=_DS)
    assert row["char_start"] is None
    assert row["char_end"] is None


def test_row_values_leaves_embedding_and_tsv_unset():
    # embedding is a follow-up (NULL now); content_tsv is Postgres-generated.
    row = chunk_row_values(_chunk(), org_id=1, data_source_id=_DS)
    assert "embedding" not in row
    assert "embedding_version" not in row
    assert "content_tsv" not in row


def test_chunk_id_is_deterministic_per_document_and_order():
    # Re-analysis re-ingesting the same document reproduces the same id, so
    # ON CONFLICT DO NOTHING makes the re-ingest a no-op.
    c = _chunk(order=3)
    assert chunk_id(_DS, c) == chunk_id(_DS, c)
    assert isinstance(chunk_row_values(c, org_id=1, data_source_id=_DS)["id"], uuid.UUID)


def test_chunk_id_differs_by_order_and_by_document():
    other_ds = uuid.uuid4()
    assert chunk_id(_DS, _chunk(order=3)) != chunk_id(_DS, _chunk(order=4))
    assert chunk_id(_DS, _chunk(order=3)) != chunk_id(other_ds, _chunk(order=3))
