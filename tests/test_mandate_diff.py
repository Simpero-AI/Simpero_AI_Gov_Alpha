"""_diff_mandate must key entries by a stable id (category_id, then slug)
when present, not just the mutable `category` display-name string -- a
category rename between two saves would otherwise make an untouched entry
look like "removed from the old name, added under the new name" in the
audit payload. category_id is what the Web frontend actually sends today
(src/lib/mandateSelection.ts's toMandateItems) -- slug is a fallback for
once the frontend starts sending it too."""

from app.api.mandates import _diff_mandate


def test_untouched_category_with_matching_category_id_produces_no_diff():
    old = [{"category_id": "c1", "category": "Investment Stage", "options": [{"option": "Seed"}]}]
    new = [
        {
            "category_id": "c1",
            "category": "Investment Stage (renamed)",
            "options": [{"option": "Seed"}],
        }
    ]
    assert _diff_mandate(old, new) == []


def test_real_change_with_category_id_still_detected_and_uses_current_label():
    old = [{"category_id": "c1", "category": "Investment Stage", "options": [{"option": "Seed"}]}]
    new = [
        {
            "category_id": "c1",
            "category": "Stage",
            "options": [{"option": "Seed"}, {"option": "Series A"}],
        }
    ]
    diff = _diff_mandate(old, new)
    assert diff == [{"category": "Stage", "type": "options", "added": ["Series A"]}]


def test_untouched_category_with_matching_slug_produces_no_diff():
    old = [
        {
            "slug": "investment_stage",
            "category": "Investment Stage",
            "options": [{"option": "Seed"}],
        }
    ]
    new = [
        {
            "slug": "investment_stage",
            "category": "Investment Stage (renamed)",
            "options": [{"option": "Seed"}],
        }
    ]
    assert _diff_mandate(old, new) == []


def test_category_id_takes_priority_over_slug():
    old = [
        {
            "category_id": "c1",
            "slug": "investment_stage",
            "category": "Investment Stage",
            "options": [{"option": "Seed"}],
        }
    ]
    # Same slug, different category_id -- treated as a different entry (id wins).
    new = [
        {
            "category_id": "c2",
            "slug": "investment_stage",
            "category": "Investment Stage",
            "options": [{"option": "Seed"}],
        }
    ]
    diff = _diff_mandate(old, new)
    assert diff == [
        {"category": "Investment Stage", "type": "options", "removed": ["Seed"]},
        {"category": "Investment Stage", "type": "options", "added": ["Seed"]},
    ]


def test_no_stable_id_falls_back_to_category_text_keying():
    old = [{"category": "Deal Types", "options": [{"option": "Buyout"}]}]
    new = [{"category": "Deal Types", "options": [{"option": "Buyout"}, {"option": "Growth"}]}]
    diff = _diff_mandate(old, new)
    assert diff == [{"category": "Deal Types", "type": "options", "added": ["Growth"]}]


def test_no_stable_id_and_renamed_category_is_misattributed():
    """Documents the pre-fix failure mode for entries that still lack both
    category_id and slug (malformed/legacy data) -- not a behavior we can
    eliminate without a stable id on the entry."""
    old = [{"category": "Sector", "options": [{"option": "Fintech"}]}]
    new = [{"category": "Sectors", "options": [{"option": "Fintech"}]}]
    diff = _diff_mandate(old, new)
    assert diff == [
        {"category": "Sector", "type": "options", "removed": ["Fintech"]},
        {"category": "Sectors", "type": "options", "added": ["Fintech"]},
    ]
