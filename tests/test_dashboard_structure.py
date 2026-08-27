"""Merge of per-document dashboard structures into one per-deal structure."""

from app.services.dashboard_structure import merge_dashboard_structures


def _consolidated(struct: dict) -> dict:
    return next(s for s in struct["subjects"] if s["kind"] == "consolidated")


def test_none_when_nothing_organizable() -> None:
    assert merge_dashboard_structures([]) is None
    assert merge_dashboard_structures([None, {}, "nonsense", 7]) is None
    assert merge_dashboard_structures([{"subjects": [], "metric_order": []}]) is None


def test_single_document_passes_through() -> None:
    out = merge_dashboard_structures(
        [
            {
                "subjects": [
                    {"name": "Consolidated", "kind": "consolidated", "entities": ["Acme Corp"]},
                    {"name": "North", "kind": "segment", "entities": ["North Region"]},
                ],
                "metric_order": ["revenue", "ebitda"],
            }
        ]
    )
    assert out is not None
    assert [s["name"] for s in out["subjects"]] == ["Consolidated", "North"]
    assert out["metric_order"] == ["revenue", "ebitda"]


def test_exactly_one_consolidated_across_documents() -> None:
    out = merge_dashboard_structures(
        [
            {
                "subjects": [
                    {"name": "Acme (whole)", "kind": "consolidated", "entities": ["Acme"]}
                ],
                "metric_order": [],
            },
            {
                "subjects": [
                    {"name": "Acme Inc", "kind": "consolidated", "entities": ["Acme Inc"]}
                ],
                "metric_order": [],
            },
        ]
    )
    assert out is not None
    assert sum(1 for s in out["subjects"] if s["kind"] == "consolidated") == 1
    assert _consolidated(out)["entities"] == ["Acme", "Acme Inc"]  # unioned


def test_segments_merge_by_name_and_dedupe_entities() -> None:
    out = merge_dashboard_structures(
        [
            {
                "subjects": [
                    {"name": "North", "kind": "segment", "entities": ["North Region", "N1"]}
                ],
                "metric_order": ["revenue"],
            },
            {
                "subjects": [
                    {"name": "north", "kind": "segment", "entities": ["North Region", "N2"]}
                ],
                "metric_order": ["ebitda", "revenue"],
            },
        ]
    )
    assert out is not None
    north = [s for s in out["subjects"] if s["kind"] == "segment"]
    assert len(north) == 1  # "North"/"north" fold together
    assert north[0]["entities"] == ["North Region", "N1", "N2"]  # first-seen, deduped
    assert out["metric_order"] == ["revenue", "ebitda"]  # first-seen union across docs


def test_consolidated_anchor_synthesized_from_segments_only() -> None:
    # No document named a consolidated subject, but segments exist -> the page
    # still needs a whole-company anchor.
    out = merge_dashboard_structures(
        [
            {
                "subjects": [{"name": "North", "kind": "segment", "entities": ["North Region"]}],
                "metric_order": [],
            }
        ]
    )
    assert out is not None
    assert out["subjects"][0]["kind"] == "consolidated"
    assert out["subjects"][0]["entities"] == []


def test_empty_segments_are_dropped_but_consolidated_kept() -> None:
    out = merge_dashboard_structures(
        [
            {
                "subjects": [
                    {"name": "Consolidated", "kind": "consolidated", "entities": ["Acme"]},
                    {"name": "Empty Seg", "kind": "segment", "entities": []},
                ],
                "metric_order": ["revenue"],
            }
        ]
    )
    assert out is not None
    assert [s["name"] for s in out["subjects"]] == ["Consolidated"]


def test_metric_order_only_still_returns_structure() -> None:
    out = merge_dashboard_structures([{"subjects": [], "metric_order": ["revenue"]}])
    assert out == {"subjects": [], "metric_order": ["revenue"]}


def test_entity_in_consolidated_is_not_also_a_segment() -> None:
    # One document calls "Acme" the whole company; another files it under a segment.
    # The consolidated total wins -> the segment must not also carry it.
    out = merge_dashboard_structures(
        [
            {
                "subjects": [{"name": "Whole", "kind": "consolidated", "entities": ["Acme"]}],
                "metric_order": [],
            },
            {
                "subjects": [
                    {"name": "North", "kind": "segment", "entities": ["Acme", "North Region"]}
                ],
                "metric_order": [],
            },
        ]
    )
    assert out is not None
    assert _consolidated(out)["entities"] == ["Acme"]
    north = next(s for s in out["subjects"] if s["name"] == "North")
    assert north["entities"] == ["North Region"]  # "Acme" not double-filed
    seen = [e for s in out["subjects"] for e in s["entities"]]
    assert len(seen) == len(set(seen))  # each entity under exactly one subject


def test_entity_in_two_segments_goes_to_the_first() -> None:
    out = merge_dashboard_structures(
        [
            {
                "subjects": [
                    {"name": "Consolidated", "kind": "consolidated", "entities": ["Acme"]},
                    {"name": "North", "kind": "segment", "entities": ["Shared Unit"]},
                    {"name": "South", "kind": "segment", "entities": ["Shared Unit"]},
                ],
                "metric_order": [],
            }
        ]
    )
    assert out is not None
    north = next(s for s in out["subjects"] if s["name"] == "North")
    assert north["entities"] == ["Shared Unit"]
    south = [s for s in out["subjects"] if s["name"] == "South"]
    assert south == []  # its only entity was already claimed by North


def test_consolidated_label_is_deterministic_first_seen() -> None:
    # Two documents name the whole company differently; the first-seen label wins
    # (last-write-wins would make the header depend on document order).
    docs = [
        {
            "subjects": [{"name": "Acme Group", "kind": "consolidated", "entities": ["A"]}],
            "metric_order": [],
        },
        {
            "subjects": [{"name": "Acme Inc", "kind": "consolidated", "entities": ["B"]}],
            "metric_order": [],
        },
    ]
    assert _consolidated(merge_dashboard_structures(docs))["name"] == "Acme Group"
    assert _consolidated(merge_dashboard_structures(list(reversed(docs))))["name"] == "Acme Inc"
