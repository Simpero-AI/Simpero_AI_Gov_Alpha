"""Unit tests for the grounded field-synthesis gate -- pure, no DB, no LLM.

The whole accuracy claim of this pass rests on _verify_points: a point survives
ONLY if it cites an excerpt id we actually retrieved, and its citations are then
mapped from those real chunks (never from the model's own words). These tests pin
that gate: invented citations are dropped, mixed citations keep only the real
ones, found=false yields nothing, and the excerpt-labelling the model cites
against is deterministic.
"""

import logging
import uuid
from types import SimpleNamespace
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import field_synthesis
from app.services.field_synthesis import (
    _MAX_NAME_CHARS,
    _MAX_PEOPLE,
    _MAX_POINT_CHARS,
    _MAX_POINTS,
    _MAX_TITLE_CHARS,
    COMPANY_SECTIONS,
    SectionSynthesis,
    SynthCitation,
    SynthPerson,
    _build_user_message,
    _verify_people,
    _verify_points,
    synthesize_company_sections,
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


def test_company_sections_include_executive_summary_with_unique_keys():
    # The Summary tab's Executive Summary is served by the same synthesis pass
    # (deal-level executive_summary section); every section key must be unique so
    # a tab can select its section by key without collision.
    keys = [s.key for s in COMPANY_SECTIONS]
    assert "executive_summary" in keys
    assert "overview" in keys and "risks" in keys and "commercial" in keys
    assert len(keys) == len(set(keys))


def _settings(*, key: str) -> SimpleNamespace:
    """Minimal stand-in for the app settings synthesize_company_sections reads --
    only the two attributes it touches. Patched in via monkeypatch so the early-
    return branch is chosen DETERMINISTICALLY (get_settings is lru_cached and CI
    sets no ANTHROPIC_API_KEY, so relying on ambient env picks the wrong branch)."""
    return SimpleNamespace(anthropic_api_key=key, field_synthesis_model="test-model")


async def test_no_documents_returns_empty_and_logs_the_reason(monkeypatch, caplog):
    # A chunk-less deal (with a key present) must fail soft to [] AND leave a
    # reason=no_documents line in the logs -- an empty page is otherwise
    # indistinguishable from a failed synthesis ("the search didn't fire"). The
    # early return never touches the session, so a dummy stands in.
    monkeypatch.setattr(field_synthesis, "get_settings", lambda: _settings(key="test-key"))
    with caplog.at_level(logging.INFO, logger="app.services.field_synthesis"):
        out = await synthesize_company_sections(
            cast(AsyncSession, object()),
            org_id="org-1",
            document_ids=[],
            company="Acme",
        )
    assert out == []
    assert any("field-synthesis skipped" in m and "no_documents" in m for m in caplog.messages)


async def test_no_api_key_returns_empty_and_logs_the_reason(monkeypatch, caplog):
    # The symmetric early return: documents present but no key -> reason=no_api_key.
    monkeypatch.setattr(field_synthesis, "get_settings", lambda: _settings(key=""))
    with caplog.at_level(logging.INFO, logger="app.services.field_synthesis"):
        out = await synthesize_company_sections(
            cast(AsyncSession, object()),
            org_id="org-1",
            document_ids=["doc-1"],
            company="Acme",
        )
    assert out == []
    assert any("field-synthesis skipped" in m and "no_api_key" in m for m in caplog.messages)


async def test_synthesize_classifies_each_empty_reason_and_logs_the_summary(monkeypatch, caplog):
    # End-to-end pin of the Wave 2 reason-code classification: drive one section
    # into each empty bucket and one into ok, and assert (1) only ok sections are
    # returned and (2) the summary log records the right per-section code. Retrieval
    # and the model are stubbed so this stays a pure unit test (no DB, no network).
    monkeypatch.setattr(field_synthesis, "get_settings", lambda: _settings(key="test-key"))

    q_of = {s.key: s.question for s in COMPANY_SECTIONS}
    plans_query = next(s.query for s in COMPANY_SECTIONS if s.key == "plans")

    async def fake_search(
        _session, *, org_id, query_text, document_ids, weights, top_k, match_mode
    ):
        # "plans" retrieves nothing (-> no_hits); every other section gets one hit.
        return [] if query_text == plans_query else [_hit(page=1)]

    def fake_call(*, api_key, model, question, company, hits, system, tool, max_tokens):
        if question == q_of["risks"]:
            return None  # no structured tool call -> model_no_tool_call
        if question == q_of["commercial"]:
            return {"found": False, "points": []}  # answered nothing -> model_no_answer
        if question == q_of["related_parties"]:
            # answered, but the only citation is an id we never retrieved -> the
            # grounding gate drops it -> ungrounded.
            return {"found": True, "points": [{"text": "invented", "chunk_ids": ["c99"]}]}
        # executive_summary + overview: a real, grounded point -> ok.
        return {"found": True, "points": [{"text": "A real grounded fact.", "chunk_ids": ["c1"]}]}

    monkeypatch.setattr(field_synthesis, "org_scoped_search", fake_search)
    monkeypatch.setattr(field_synthesis, "_call_model", fake_call)

    with caplog.at_level(logging.INFO, logger="app.services.field_synthesis"):
        out = await synthesize_company_sections(
            cast(AsyncSession, object()),
            org_id="org-1",
            document_ids=["doc-1"],
            company="Acme",
        )

    assert {s.key for s in out} == {"executive_summary", "overview"}
    summary = next(m for m in caplog.messages if "sections grounded" in m)
    assert "executive_summary=ok" in summary
    assert "overview=ok" in summary
    assert "risks=model_no_tool_call" in summary
    assert "commercial=model_no_answer" in summary
    assert "related_parties=ungrounded" in summary
    assert "plans=no_hits" in summary


def test_verifies_a_grounded_person_and_maps_citation():
    h1 = _hit(
        page=7, document_id="doc-a", content="Jane Smith is the CEO with 10 years in fintech."
    )
    raw = {
        "found": True,
        "people": [
            {
                "name": "Jane Smith",
                "title": "CEO",
                "background": "10 years in fintech",
                "chunk_ids": ["c1"],
            }
        ],
    }

    (person,) = _verify_people(raw, [h1])

    assert person.name == "Jane Smith"
    assert person.title == "CEO"
    assert person.background == "10 years in fintech"
    assert [(c.document_id, c.page) for c in person.citations] == [("doc-a", 7)]
    assert person.chunk_ids == [str(h1.chunk_id)]


def test_drops_a_person_citing_only_an_invented_id():
    h1 = _hit(page=7, content="Jane Smith is the CEO.")
    raw = {
        "found": True,
        "people": [{"name": "Jane Smith", "chunk_ids": ["c9"]}],
    }
    assert _verify_people(raw, [h1]) == []


def test_keeps_only_valid_citations_when_a_person_mixes_real_and_invented_ids():
    h1 = _hit(page=7, document_id="doc-a", content="Jane Smith is the CEO.")
    h2 = _hit(page=8, document_id="doc-a", content="Jane Smith previously worked at Acme.")
    raw = {"found": True, "people": [{"name": "Jane Smith", "chunk_ids": ["c2", "c99"]}]}

    (person,) = _verify_people(raw, [h1, h2])

    assert [(c.document_id, c.page) for c in person.citations] == [("doc-a", 8)]
    assert person.chunk_ids == [str(h2.chunk_id)]


def test_people_found_false_yields_nothing():
    h1 = _hit(page=1, content="Jane Smith is the CEO.")
    raw = {"found": False, "people": [{"name": "Jane Smith", "chunk_ids": ["c1"]}]}
    assert _verify_people(raw, [h1]) == []


def test_dedups_person_by_name_case_and_whitespace():
    h1 = _hit(page=1, content="Jane Smith is the CEO.")
    raw = {
        "found": True,
        "people": [
            {"name": "Jane   Smith", "chunk_ids": ["c1"]},
            {"name": "jane smith", "chunk_ids": ["c1"]},
        ],
    }
    assert len(_verify_people(raw, [h1])) == 1


def test_drops_empty_or_missing_name():
    h1 = _hit(page=1, content="Some content.")
    raw = {
        "found": True,
        "people": [{"name": "   ", "chunk_ids": ["c1"]}, {"chunk_ids": ["c1"]}],
    }
    assert _verify_people(raw, [h1]) == []


def test_drops_overlong_name_title_or_background():
    h1 = _hit(page=1, content="Jane Smith bio.")
    raw = {
        "found": True,
        "people": [
            {"name": "x" * (_MAX_NAME_CHARS + 1), "chunk_ids": ["c1"]},
            {"name": "Jane Smith", "title": "x" * (_MAX_TITLE_CHARS + 1), "chunk_ids": ["c1"]},
            {"name": "Jane Smith", "background": "x" * (_MAX_POINT_CHARS + 1), "chunk_ids": ["c1"]},
        ],
    }
    assert _verify_people(raw, [h1]) == []


def test_caps_number_of_people():
    hits = [
        _hit(page=i + 1, content=f"Person Lastname{i} works here.") for i in range(_MAX_PEOPLE + 5)
    ]
    raw = {
        "found": True,
        "people": [
            {"name": f"Person Lastname{i}", "chunk_ids": [f"c{i + 1}"]} for i in range(len(hits))
        ],
    }
    assert len(_verify_people(raw, hits)) == _MAX_PEOPLE


def test_malformed_people_raw_yields_nothing():
    h1 = _hit(page=1)
    assert _verify_people(None, [h1]) == []
    assert _verify_people({"found": True, "people": "not-a-list"}, [h1]) == []
    assert _verify_people({"found": True, "people": ["not-a-dict"]}, [h1]) == []


def test_person_empty_title_and_background_become_none():
    h1 = _hit(page=1, content="Jane Smith leads the company.")
    raw = {
        "found": True,
        "people": [{"name": "Jane Smith", "title": "   ", "background": "", "chunk_ids": ["c1"]}],
    }
    (person,) = _verify_people(raw, [h1])
    assert person.title is None
    assert person.background is None


def test_drops_a_person_whose_surname_is_not_in_the_cited_chunk_text():
    # Citation is structurally valid (cites a real, retrieved chunk) but the
    # chunk's text never actually mentions "Smith" -- the model attached the
    # wrong citation (or invented the surname). The citation-grounding gate
    # alone would let this through; only the surname-presence gate catches it.
    h1 = _hit(page=7, content="The CEO has twenty years of industry experience.")
    raw = {"found": True, "people": [{"name": "Jane Smith", "chunk_ids": ["c1"]}]}
    assert _verify_people(raw, [h1]) == []


async def test_leadership_section_reaches_ok_via_people_only_result(monkeypatch, caplog):
    # The leadership section reports via `people`, not `points` -- pin that the
    # reason-code classification treats a people-only result as "ok" (grounded),
    # the same way a points-only result is for every other section.
    monkeypatch.setattr(field_synthesis, "get_settings", lambda: _settings(key="test-key"))
    q_of = {s.key: s.question for s in COMPANY_SECTIONS}

    async def fake_search(
        _session, *, org_id, query_text, document_ids, weights, top_k, match_mode
    ):
        return [_hit(page=1, content="Jane Smith is the CEO.")]

    def fake_call(*, api_key, model, question, company, hits, system, tool, max_tokens):
        if question == q_of["leadership"]:
            return {
                "found": True,
                "people": [{"name": "Jane Smith", "title": "CEO", "chunk_ids": ["c1"]}],
            }
        return {"found": False, "points": []}

    monkeypatch.setattr(field_synthesis, "org_scoped_search", fake_search)
    monkeypatch.setattr(field_synthesis, "_call_model", fake_call)

    with caplog.at_level(logging.INFO, logger="app.services.field_synthesis"):
        out = await synthesize_company_sections(
            cast(AsyncSession, object()),
            org_id="org-1",
            document_ids=["doc-1"],
            company="Acme",
        )

    (section,) = [s for s in out if s.key == "leadership"]
    assert section.points == []
    assert [p.name for p in section.people] == ["Jane Smith"]
    summary = next(m for m in caplog.messages if "sections grounded" in m)
    assert "leadership=ok" in summary


def test_sections_round_trip_preserves_people():
    section = SectionSynthesis(
        key="leadership",
        title="Leadership",
        people=[
            SynthPerson(
                name="Jane Smith",
                title="CEO",
                background="Ex-Acme",
                citations=[SynthCitation(document_id="doc-a", page=3)],
                chunk_ids=["chunk-1"],
            )
        ],
    )
    (restored,) = field_synthesis.sections_from_json(field_synthesis.sections_to_json([section]))
    assert restored.people == section.people
    assert restored.points == []


def test_old_shape_section_without_people_key_deserializes_to_empty_list():
    old_row = {"key": "overview", "title": "Business Overview", "points": []}
    (restored,) = field_synthesis.sections_from_json([old_row])
    assert restored.people == []
