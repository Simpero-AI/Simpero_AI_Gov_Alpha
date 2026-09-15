"""Pure-logic tests for the W3 synthesis-snapshot round-trip and reason sentinel.

The persist stage serializes the synthesis result to JSONB (sections_to_json) and
the GET deserializes it back (sections_from_json); if that round-trip loses or
corrupts a point/citation, the frozen snapshot renders wrong. These tests pin the
round-trip and the tolerance of a malformed row (must fail soft, never raise), plus
the deal-level reason sentinel. The DB-backed tests (stage writes a row, GET reads
latest + invokes no LLM/retrieval, RLS isolation, re-analysis supersession) need a
Postgres and run in CI / on staging.
"""

from app.services.field_synthesis import (
    SectionSynthesis,
    SynthCitation,
    SynthPoint,
    sections_from_json,
    sections_to_json,
    snapshot_reason,
)


def _section() -> SectionSynthesis:
    return SectionSynthesis(
        key="overview",
        title="Business Overview",
        points=[
            SynthPoint(
                text="The company sells cloud software.",
                citations=[
                    SynthCitation(document_id="doc-a", page=7),
                    SynthCitation(document_id="doc-b", page=None),
                ],
                chunk_ids=["chunk-1", "chunk-2"],
            )
        ],
    )


def test_section_round_trips_through_json_unchanged():
    section = _section()
    restored = SectionSynthesis.from_json(section.to_json())
    assert restored == section


def test_citation_preserves_none_page():
    # A page-less chunk (table/chart) still grounded the point; the None must survive
    # the JSONB round-trip, not become 0 or "".
    c = SynthCitation(document_id="doc-a", page=None)
    assert SynthCitation.from_json(c.to_json()) == c


def test_sections_to_and_from_json_round_trip():
    sections = [_section(), SectionSynthesis(key="risks", title="Risks", points=[])]
    assert sections_from_json(sections_to_json(sections)) == sections


def test_sections_from_json_is_tolerant_of_a_malformed_row():
    # A corrupt/legacy snapshot must fail soft to what it can parse (the FE then
    # falls back to the claims view), never raise and 500 the page.
    assert sections_from_json(None) == []
    assert sections_from_json("not-a-list") == []
    assert sections_from_json([1, "x", None]) == []  # non-dict items skipped
    # Missing keys default rather than KeyError.
    partial = sections_from_json([{"key": "overview"}])
    assert partial == [SectionSynthesis(key="overview", title="", points=[])]


def test_point_from_json_tolerates_missing_and_malformed_fields():
    p = SynthPoint.from_json({"text": "x", "citations": "nope", "chunk_ids": None})
    assert p == SynthPoint(text="x", citations=[], chunk_ids=[])


def test_snapshot_reason_covers_each_empty_path_and_ok():
    ok_sections = [_section()]
    assert (
        snapshot_reason(has_api_key=False, has_documents=True, sections=ok_sections) == "no_api_key"
    )
    assert (
        snapshot_reason(has_api_key=True, has_documents=False, sections=ok_sections)
        == "no_documents"
    )
    assert (
        snapshot_reason(has_api_key=True, has_documents=True, sections=[]) == "no_sections_grounded"
    )
    assert snapshot_reason(has_api_key=True, has_documents=True, sections=ok_sections) == "ok"


def test_snapshot_reason_values_are_all_in_the_model_check():
    # The reason must always be one of the CHECK-constrained values, or the INSERT
    # violates ck_synthesis_snapshot_reason.
    from app.models.synthesis_snapshot import REASONS

    produced = {
        snapshot_reason(has_api_key=k, has_documents=d, sections=s)
        for k in (True, False)
        for d in (True, False)
        for s in ([], [_section()])
    }
    assert produced <= set(REASONS)
