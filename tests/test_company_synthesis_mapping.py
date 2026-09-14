"""Unit tests for _synthesis_to_response -- the pure mapping from grounded
field_synthesis sections to the company-synthesis wire shape (citation
formatting). The endpoint itself is a thin wrapper around synthesize_company_sections
(fail-soft) + this mapper."""

from app.api.deals import _synthesis_to_response
from app.services.field_synthesis import SectionSynthesis, SynthCitation, SynthPoint

_DOC_A = "11111111-1111-1111-1111-111111111111"
_DOC_B = "22222222-2222-2222-2222-222222222222"
_FILENAMES = {_DOC_A: "apple-10k-2024.pdf", _DOC_B: "apple-investor-deck.pdf"}


def _section(*points: SynthPoint) -> SectionSynthesis:
    return SectionSynthesis(key="overview", title="Business Overview", points=list(points))


def test_maps_a_single_cited_point_to_file_and_page():
    section = _section(
        SynthPoint(
            text="Apple designs consumer devices and sells recurring services.",
            citations=[SynthCitation(document_id=_DOC_A, page=3)],
            chunk_ids=["c1"],
        )
    )
    resp = _synthesis_to_response([section], _FILENAMES)
    (out,) = resp.sections
    assert out.key == "overview"
    assert out.title == "Business Overview"
    (point,) = out.points
    assert point.text == "Apple designs consumer devices and sells recurring services."
    assert point.citation == "apple-10k-2024.pdf · p.3"


def test_dedupes_repeated_citations_and_joins_multiple():
    section = _section(
        SynthPoint(
            text="Services is the fastest-growing segment.",
            citations=[
                SynthCitation(document_id=_DOC_A, page=3),
                SynthCitation(document_id=_DOC_A, page=3),  # duplicate -> collapsed
                SynthCitation(document_id=_DOC_B, page=12),
            ],
            chunk_ids=["c1", "c2"],
        )
    )
    (out,) = _synthesis_to_response([section], _FILENAMES).sections
    assert out.points[0].citation == "apple-10k-2024.pdf · p.3; apple-investor-deck.pdf · p.12"


def test_page_less_citation_renders_as_filename_only():
    section = _section(
        SynthPoint(
            text="Revenue by segment is shown in the chart.",
            citations=[SynthCitation(document_id=_DOC_B, page=None)],
            chunk_ids=["c9"],
        )
    )
    (out,) = _synthesis_to_response([section], _FILENAMES).sections
    assert out.points[0].citation == "apple-investor-deck.pdf"


def test_no_citations_yields_null_citation():
    section = _section(
        SynthPoint(text="Grounded but source unresolved.", citations=[], chunk_ids=[])
    )
    (out,) = _synthesis_to_response([section], _FILENAMES).sections
    assert out.points[0].citation is None


def test_unknown_document_id_falls_back_to_the_id():
    section = _section(
        SynthPoint(
            text="From a source not in the filenames map.",
            citations=[SynthCitation(document_id="deadbeef", page=5)],
            chunk_ids=["c1"],
        )
    )
    (out,) = _synthesis_to_response([section], _FILENAMES).sections
    assert out.points[0].citation == "deadbeef · p.5"


def test_empty_sections_map_to_empty_response():
    assert _synthesis_to_response([], _FILENAMES).sections == []
