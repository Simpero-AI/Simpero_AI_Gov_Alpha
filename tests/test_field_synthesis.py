"""Unit tests for the grounded field-synthesis gate -- pure, no DB, no LLM.

The whole accuracy claim of this pass rests on _verify_points: a point survives
ONLY if it cites an excerpt id we actually retrieved, and its citations are then
mapped from those real chunks (never from the model's own words). These tests pin
that gate: invented citations are dropped, mixed citations keep only the real
ones, found=false yields nothing, and the excerpt-labelling the model cites
against is deterministic.
"""

import uuid

from app.services.field_synthesis import (
    _MAX_POINT_CHARS,
    _MAX_POINTS,
    _build_user_message,
    _verify_points,
)
from app.services.retrieval import ChunkHit


def _hit(
    *, page: int | None, document_id: str = "doc-a", content: str = "excerpt text"
) -> ChunkHit:
    return ChunkHit(
        # runtime chunk_id is a UUID; ChunkHit annotates it int (see retrieval.py).
        chunk_id=str(uuid.uuid4()),  # type: ignore[arg-type]
        content=content,
        document_id=document_id,
        page=page,  # type: ignore[arg-type]
        char_start=None,
        char_end=None,
        element_type="prose",
        score=1.0,
    )


def test_verifies_a_grounded_point_and_maps_its_citation():
    h1 = _hit(page=7, document_id="doc-a")
    h2 = _hit(page=13, document_id="doc-b")
    raw = {
        "found": True,
        "points": [{"text": "Customers span consumer and enterprise.", "chunk_ids": ["c1"]}],
    }

    (point,) = _verify_points(raw, [h1, h2])

    assert point.text == "Customers span consumer and enterprise."
    assert [(c.document_id, c.page) for c in point.citations] == [("doc-a", 7)]
    assert point.chunk_ids == [str(h1.chunk_id)]


def test_drops_a_point_that_cites_only_an_invented_id():
    # The model cited an id we never retrieved -> ungrounded -> the point is gone.
    h1 = _hit(page=7)
    raw = {"found": True, "points": [{"text": "Revenue was $500B.", "chunk_ids": ["c9"]}]}
    assert _verify_points(raw, [h1]) == []


def test_keeps_only_the_valid_citations_when_a_point_mixes_real_and_invented_ids():
    h1 = _hit(page=7, document_id="doc-a")
    h2 = _hit(page=8, document_id="doc-a")
    raw = {"found": True, "points": [{"text": "It operates globally.", "chunk_ids": ["c2", "c99"]}]}

    (point,) = _verify_points(raw, [h1, h2])

    # c2 is real (-> h2, p.8); c99 was invented and contributes no citation.
    assert [(c.document_id, c.page) for c in point.citations] == [("doc-a", 8)]
    assert point.chunk_ids == [str(h2.chunk_id)]


def test_found_false_yields_no_points_even_if_the_model_returned_some():
    h1 = _hit(page=1)
    raw = {"found": False, "points": [{"text": "Something.", "chunk_ids": ["c1"]}]}
    assert _verify_points(raw, [h1]) == []


def test_dedups_identical_point_text():
    h1 = _hit(page=1)
    raw = {
        "found": True,
        "points": [
            {"text": "The company sells software.", "chunk_ids": ["c1"]},
            {"text": "The company sells software.", "chunk_ids": ["c1"]},
        ],
    }
    assert len(_verify_points(raw, [h1])) == 1


def test_dedups_citations_by_document_and_page():
    # Two retrieved chunks on the same doc+page -> one display citation, both
    # chunk_ids retained for provenance.
    h1 = _hit(page=5, document_id="doc-a")
    h2 = _hit(page=5, document_id="doc-a")
    raw = {"found": True, "points": [{"text": "A fact.", "chunk_ids": ["c1", "c2"]}]}

    (point,) = _verify_points(raw, [h1, h2])

    assert [(c.document_id, c.page) for c in point.citations] == [("doc-a", 5)]
    assert point.chunk_ids == [str(h1.chunk_id), str(h2.chunk_id)]


def test_caps_the_number_of_points():
    hits = [_hit(page=i + 1) for i in range(_MAX_POINTS + 5)]
    raw = {
        "found": True,
        "points": [
            {"text": f"Distinct fact number {i}.", "chunk_ids": [f"c{i + 1}"]}
            for i in range(len(hits))
        ],
    }
    assert len(_verify_points(raw, hits)) == _MAX_POINTS


def test_drops_empty_or_overlong_text():
    h1 = _hit(page=1)
    raw = {
        "found": True,
        "points": [
            {"text": "   ", "chunk_ids": ["c1"]},
            {"text": "x" * (_MAX_POINT_CHARS + 1), "chunk_ids": ["c1"]},
        ],
    }
    assert _verify_points(raw, [h1]) == []


def test_malformed_raw_yields_nothing():
    h1 = _hit(page=1)
    assert _verify_points(None, [h1]) == []
    assert _verify_points({"found": True, "points": "not-a-list"}, [h1]) == []
    assert _verify_points({"found": True, "points": ["not-a-dict"]}, [h1]) == []


def test_build_user_message_labels_excerpts_with_ids_and_pages():
    h1 = _hit(page=7, content="Apple sells iPhones.")
    h2 = _hit(page=None, content="A table with no page.")
    msg = _build_user_message(question="What does it do?", company="Apple", hits=[h1, h2])

    assert "QUESTION: What does it do?" in msg
    assert "[c1] (p.7) Apple sells iPhones." in msg
    assert "[c2] (n/a) A table with no page." in msg
