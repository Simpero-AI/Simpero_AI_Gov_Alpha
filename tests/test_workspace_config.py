"""SIM-414: app/services/screening/workspace_config.py -- gs_07/gs_08's
approved geography/sector lists, now derived from the org's own `mandates`
row (the one PUT /api/mandate writes) instead of the writer-less
`investment_profiles.mandate` it used to read.

Most of this file is DB-free: the blob transform is a pure function, so the
containment rules and the whole malformed-input matrix are exercised without
Postgres. Only the loader tests (bottom) need db_session + a seeded taxonomy.
"""

from __future__ import annotations

import uuid

import pytest

from app.repo.MandateCategoryRepo import MandateCategoryRepo
from app.repo.MandateOptionsRepo import MandateOptionsRepo
from app.repo.MandateRepo import MandateRepo
from app.services.screening.workspace_config import (
    GEOGRAPHY,
    SECTOR,
    WorkspaceConfig,
    _approved_labels,
    _build_index,
    _CategoryIndex,
    _matching_entries,
    load_workspace_config,
    normalize_label,
)

# --- the category-identity contract ---------------------------------------


def test_slugs_match_the_backend_owned_enum():
    """workspace_config copies these two slug strings rather than importing
    the enum, because that enum is an admin-portal schema and admin/product
    code don't share modules (CLAUDE.md). This is the drift guard that makes
    the copy safe -- same idea as tests/test_parse_client.py pinning
    PARSE_QUEUE_NAME."""
    from app.schemas.admin.mandate import MandateCategorySlug

    assert GEOGRAPHY.slug == MandateCategorySlug.GEOGRAPHIES.value
    assert SECTOR.slug == MandateCategorySlug.TARGET_SECTORS.value


def test_canonical_labels_are_covered_by_the_name_fallback():
    """A slug-less category created with the canonical display name must still
    be found by the name fallback."""
    from app.schemas.admin.mandate import CANONICAL_LABELS, MandateCategorySlug

    geographies = CANONICAL_LABELS[MandateCategorySlug.GEOGRAPHIES]
    sectors = CANONICAL_LABELS[MandateCategorySlug.TARGET_SECTORS]

    assert normalize_label(geographies) in GEOGRAPHY.name_aliases
    assert normalize_label(sectors) in SECTOR.name_aliases


# --- normalize_label -------------------------------------------------------


@pytest.mark.parametrize(
    "left,right",
    [
        ("Canada", "canada"),
        ("Canada", "  CANADA  "),
        ("United States", "united   states"),
        ("SaaS", "saas"),
        ("Québec", "Québec"),  # decomposed vs composed, NFKC folds both
    ],
)
def test_normalize_label_folds(left, right):
    assert normalize_label(left) == normalize_label(right)


def test_normalize_label_does_not_alias():
    """The deliberate non-goal: no synonym table. "US" and "United States"
    are different strings and stay different."""
    assert normalize_label("US") != normalize_label("United States")


# --- WorkspaceConfig matching ---------------------------------------------


def test_approves_folds_but_keeps_raw_strings():
    config = WorkspaceConfig(approved_sectors=["SaaS"], approved_geographies=["United States"])

    assert config.approves_sector("saas")
    assert config.approves_geography("  UNITED   STATES ")
    assert not config.approves_geography("US")
    # The raw display strings survive -- they're what a human reads.
    assert config.approved_geographies == ["United States"]


def test_approves_is_false_when_the_policy_is_unset():
    """Total function: unset never raises. The evaluators check `is None`
    first, to tell "unset" (unknown) from "not approved" (N)."""
    config = WorkspaceConfig(approved_sectors=None, approved_geographies=None)

    assert not config.approves_sector("saas")
    assert not config.approves_geography("Canada")


# --- the transform: containment -------------------------------------------


def _index(*, parents: dict[str, list[str]] | None = None) -> _CategoryIndex:
    """Build a taxonomy index straight from names, so the containment tests
    read as the tree they describe. Returns the index plus stable ids via the
    returned `by_name` map."""

    class _Row:
        def __init__(self, id_, option, parent_option_id):
            self.id = id_
            self.option = option
            self.parent_option_id = parent_option_id

    rows = []
    for parent, children in (parents or {}).items():
        parent_id = uuid.uuid4()
        rows.append(_Row(parent_id, parent, None))
        for child in children:
            rows.append(_Row(uuid.uuid4(), child, parent_id))
    return _build_index(rows)  # type: ignore[arg-type]


def _entry(category: str, *options: dict) -> dict:
    return {"category": category, "options": list(options)}


def test_parent_with_no_sub_options_approves_its_whole_subtree():
    """ "Only deals from Canada" must pass a deal whose HQ is recorded at the
    province level. sub_options is omitted when empty, so an absent key means
    "all of it"."""
    index = _index(parents={"Canada": ["British Columbia", "Ontario"], "France": []})
    entries = [_entry("Geographies", {"option": "Canada"})]

    labels = _approved_labels(entries, index)

    assert labels is not None
    assert set(labels) == {"Canada", "British Columbia", "Ontario"}
    assert "France" not in labels


def test_parent_with_listed_sub_options_approves_only_those():
    index = _index(parents={"Canada": ["British Columbia", "Ontario"]})
    entries = [
        _entry(
            "Geographies",
            {"option": "Canada", "sub_options": [{"option": "British Columbia"}]},
        )
    ]

    labels = _approved_labels(entries, index)

    assert labels is not None
    assert set(labels) == {"Canada", "British Columbia"}
    assert "Ontario" not in labels


def test_empty_sub_options_reads_as_omitted():
    """A saved blob omits the key when empty, so [] cannot mean "explicitly
    zero children" -- it has to mean the same as absent."""
    index = _index(parents={"Canada": ["Ontario"]})

    labels = _approved_labels(
        [_entry("Geographies", {"option": "Canada", "sub_options": []})], index
    )

    assert labels is not None
    assert set(labels) == {"Canada", "Ontario"}


def test_expansion_survives_a_cycle_in_parent_option_id():
    """The self-FK permits a cycle; an infinite loop inside the screening job
    would be a far worse failure than a skipped node."""
    a, b = uuid.uuid4(), uuid.uuid4()
    index = _CategoryIndex(
        children={a: [b], b: [a]},
        labels={a: "Canada", b: "Ontario"},
        by_name={"canada": a, "ontario": b},
    )

    labels = _approved_labels([_entry("Geographies", {"option": "Canada"})], index)

    assert labels is not None
    assert set(labels) == {"Canada", "Ontario"}


def test_option_absent_from_the_taxonomy_is_still_approved_literally():
    """An admin renaming/deleting an option orphans the saved blob. The
    literal string keeps matching; it just can't be expanded."""
    labels = _approved_labels([_entry("Geographies", {"option": "Atlantis"})], _index())

    assert labels == ["Atlantis"]


def test_duplicates_dedupe_to_the_first_spelling():
    index = _index(parents={"Canada": ["Ontario"]})
    entries = [_entry("Geographies", {"option": "Canada"}, {"option": "  canada  "})]

    assert _approved_labels(entries, index) == ["Canada", "Ontario"]


# --- the transform: the three-state contract ------------------------------


def test_category_absent_entirely_is_unset():
    entries = _matching_entries([{"category": "Investment Stage", "options": []}], GEOGRAPHY, set())
    assert _approved_labels(entries, _index()) is None


def test_empty_but_present_category_is_unset_not_an_empty_policy():
    """SIM-414 decision: in this blob an empty options list is
    indistinguishable from "the Builder rendered the category and nothing was
    ticked". Reading it as a deliberate "approve nothing" policy would flip
    every deal to N against a policy nobody wrote."""
    assert _approved_labels([_entry("Geographies")], _index()) is None


def test_all_junk_options_are_unset_not_an_empty_policy():
    """Something was configured but none of it parsed. Emitting a definite N
    would put "we checked; this HQ is not approved" in an audit trail on the
    basis of data we could not read."""
    entries = [_entry("Geographies", {"no_option_key": True}, {"option": "   "})]

    assert _approved_labels(entries, _index()) is None


# --- the transform: category matching -------------------------------------


def test_entry_matched_by_category_id_wins_over_the_display_name():
    """category_id is the key the frontend always sends and the one an admin
    rename can't break."""
    category_id = str(uuid.uuid4())
    blob = [{"category_id": category_id, "category": "Renamed By An Admin", "options": []}]

    assert _matching_entries(blob, GEOGRAPHY, {category_id}) == blob


def test_entry_with_a_foreign_category_id_does_not_fall_back_to_the_name():
    """An id that resolves to a different category is a definite answer, not a
    reason to keep guessing."""
    blob = [{"category_id": str(uuid.uuid4()), "category": "Geographies", "options": []}]

    assert _matching_entries(blob, GEOGRAPHY, {str(uuid.uuid4())}) == []


def test_entry_matched_by_slug_when_no_category_id():
    blob = [{"slug": "target_sectors", "category": "Whatever", "options": []}]

    assert _matching_entries(blob, SECTOR, set()) == blob
    assert _matching_entries(blob, GEOGRAPHY, set()) == []


@pytest.mark.parametrize(
    "category", ["Target Sectors", "Sectors", "Sector", "SECTORS", "  target   sectors  "]
)
def test_sector_category_display_name_aliases(category):
    blob = [{"category": category, "options": []}]

    assert _matching_entries(blob, SECTOR, set()) == blob


def test_unrelated_category_name_does_not_match():
    blob = [{"category": "Sectorz", "options": []}]

    assert _matching_entries(blob, SECTOR, set()) == []


def test_two_matching_entries_are_unioned_not_overwritten():
    index = _index(parents={"SaaS": [], "Fintech": []})
    blob = [
        {"category": "Sectors", "options": [{"option": "SaaS"}]},
        {"category": "Target Sectors", "options": [{"option": "Fintech"}]},
    ]

    labels = _approved_labels(_matching_entries(blob, SECTOR, set()), index)

    assert labels is not None
    assert set(labels) == {"SaaS", "Fintech"}


# --- the transform: malformed input never raises --------------------------


@pytest.mark.parametrize(
    "blob",
    [
        None,
        "not a list",
        42,
        [None],
        ["a string entry"],
        [42],
        [{}],
        [{"category": 42, "options": []}],
        [{"category": None}],
    ],
)
def test_malformed_blob_shapes_are_skipped_not_raised_on(blob):
    """The blob is unvalidated `list[Any]` on the way in, and this runs inside
    the screening job -- one bad entry must not fail the whole analysis_run."""
    assert _approved_labels(_matching_entries(blob, GEOGRAPHY, set()), _index()) is None


@pytest.mark.parametrize(
    "options",
    [
        "not a list",
        42,
        {"option": "Canada"},  # dict, not a list of dicts
    ],
)
def test_non_list_options_is_skipped(options):
    blob = [{"category": "Geographies", "options": options}]

    assert _approved_labels(_matching_entries(blob, GEOGRAPHY, set()), _index()) is None


@pytest.mark.parametrize(
    "item",
    [
        None,
        "Canada",
        42,
        {},
        {"option": None},
        {"option": 42},
        {"option": ""},
        {"option": "   "},
    ],
)
def test_unusable_option_items_are_skipped_and_the_rest_survive(item):
    blob = [{"category": "Geographies", "options": [item, {"option": "Canada"}]}]

    labels = _approved_labels(_matching_entries(blob, GEOGRAPHY, set()), _index())

    assert labels == ["Canada"]


def test_check_size_entry_is_ignored_without_warning_noise():
    """The check-size row legitimately has no `options` key at all."""
    blob = [{"category": "Check Size Range", "min": 5000000, "max": 10000000}]

    assert _matching_entries(blob, GEOGRAPHY, set()) == []
    assert _matching_entries(blob, SECTOR, set()) == []


@pytest.mark.parametrize("option_id", [None, "not-a-uuid", 42, ""])
def test_unusable_option_id_falls_back_to_the_name(option_id):
    index = _index(parents={"Canada": ["Ontario"]})
    item = {"option": "Canada"}
    if option_id is not None:
        item["option_id"] = option_id

    labels = _approved_labels([_entry("Geographies", item)], index)

    assert labels is not None
    assert set(labels) == {"Canada", "Ontario"}  # still expanded, via the name


@pytest.mark.parametrize("sub_options", ["not a list", 42, {"option": "BC"}])
def test_malformed_sub_options_reads_as_omitted(sub_options):
    index = _index(parents={"Canada": ["British Columbia", "Ontario"]})
    entries = [_entry("Geographies", {"option": "Canada", "sub_options": sub_options})]

    labels = _approved_labels(entries, index)

    assert labels is not None
    assert set(labels) == {"Canada", "British Columbia", "Ontario"}


def test_malformed_sub_option_items_keep_the_parent():
    index = _index(parents={"Canada": ["British Columbia"]})
    entries = [
        _entry(
            "Geographies",
            {"option": "Canada", "sub_options": [None, {"option": "British Columbia"}]},
        )
    ]

    labels = _approved_labels(entries, index)

    assert labels is not None
    assert set(labels) == {"Canada", "British Columbia"}


# --- the loader, against real Postgres ------------------------------------


async def _seed_category(db_session, category: str, slug: str | None) -> uuid.UUID:
    row = await MandateCategoryRepo(db_session).create({"category": category, "slug": slug})
    await db_session.flush()
    return row.id


async def _seed_option(
    db_session, category_id: uuid.UUID, option: str, parent_option_id: uuid.UUID | None = None
) -> uuid.UUID:
    row = await MandateOptionsRepo(db_session).create(
        {"category_id": category_id, "option": option, "parent_option_id": parent_option_id}
    )
    await db_session.flush()
    return row.id


async def _seed_mandate(db_session, org_a_id, user_a_id, blob) -> None:
    await MandateRepo(db_session).create(
        {"org_id": org_a_id, "user_id": user_a_id, "mandate": blob}
    )
    await db_session.flush()


async def test_no_mandate_row_is_unconfigured(db_session):
    config = await load_workspace_config(db_session)

    assert config.approved_sectors is None
    assert config.approved_geographies is None


@pytest.mark.parametrize("blob", [None, []])
async def test_empty_mandate_is_unconfigured(db_session, org_a_id, user_a_id, blob):
    await _seed_mandate(db_session, org_a_id, user_a_id, blob)

    config = await load_workspace_config(db_session)

    assert config.approved_sectors is None
    assert config.approved_geographies is None


async def test_one_category_configured_leaves_the_other_unset(db_session, org_a_id, user_a_id):
    """Per-category independence: filling in geographies says nothing about
    the sector policy, which must stay `unknown` rather than becoming N."""
    category_id = await _seed_category(
        db_session, f"Geographies {uuid.uuid4().hex[:6]}", "geographies"
    )
    canada = await _seed_option(db_session, category_id, "Canada")
    await _seed_mandate(
        db_session,
        org_a_id,
        user_a_id,
        [
            {
                "category_id": str(category_id),
                "category": "Geographies",
                "options": [{"option": "Canada", "option_id": str(canada)}],
            }
        ],
    )

    config = await load_workspace_config(db_session)

    assert config.approved_geographies == ["Canada"]
    assert config.approved_sectors is None


async def test_loader_expands_sub_options_from_the_taxonomy(db_session, org_a_id, user_a_id):
    """The end of the read path, against real rows: "Canada" alone approves
    Ontario, because the taxonomy says Ontario is inside Canada."""
    category_id = await _seed_category(db_session, f"Geo {uuid.uuid4().hex[:6]}", "geographies")
    canada = await _seed_option(db_session, category_id, "Canada")
    await _seed_option(db_session, category_id, "Ontario", parent_option_id=canada)
    await _seed_option(db_session, category_id, "British Columbia", parent_option_id=canada)
    await _seed_option(db_session, category_id, "France")

    await _seed_mandate(
        db_session,
        org_a_id,
        user_a_id,
        [{"slug": "geographies", "options": [{"option": "Canada", "option_id": str(canada)}]}],
    )

    config = await load_workspace_config(db_session)

    assert config.approved_geographies is not None
    assert set(config.approved_geographies) == {"Canada", "Ontario", "British Columbia"}
    assert config.approves_geography("ontario")
    assert not config.approves_geography("France")


async def test_loader_honours_listed_sub_options(db_session, org_a_id, user_a_id):
    category_id = await _seed_category(db_session, f"Geo {uuid.uuid4().hex[:6]}", "geographies")
    canada = await _seed_option(db_session, category_id, "Canada")
    bc = await _seed_option(db_session, category_id, "British Columbia", parent_option_id=canada)
    await _seed_option(db_session, category_id, "Ontario", parent_option_id=canada)

    await _seed_mandate(
        db_session,
        org_a_id,
        user_a_id,
        [
            {
                "slug": "geographies",
                "options": [
                    {
                        "option": "Canada",
                        "option_id": str(canada),
                        "sub_options": [{"option": "British Columbia", "option_id": str(bc)}],
                    }
                ],
            }
        ],
    )

    config = await load_workspace_config(db_session)

    assert config.approved_geographies is not None
    assert set(config.approved_geographies) == {"Canada", "British Columbia"}
    assert not config.approves_geography("Ontario")


async def test_loader_resolves_the_sector_category_by_slug(db_session, org_a_id, user_a_id):
    """The canonical sector category is named "Target Sectors" -- matching on
    the slug is what stops that from being a vocabulary trap."""
    category_id = await _seed_category(
        db_session, f"Target Sectors {uuid.uuid4().hex[:6]}", "target_sectors"
    )
    saas = await _seed_option(db_session, category_id, "SaaS")
    await _seed_mandate(
        db_session,
        org_a_id,
        user_a_id,
        [
            {
                "category_id": str(category_id),
                "category": "Target Sectors",
                "options": [{"option": "SaaS", "option_id": str(saas)}],
            }
        ],
    )

    config = await load_workspace_config(db_session)

    assert config.approved_sectors == ["SaaS"]
    assert config.approves_sector("saas")


async def test_loader_ignores_the_check_size_entry(db_session, org_a_id, user_a_id):
    await _seed_mandate(
        db_session,
        org_a_id,
        user_a_id,
        [{"category": "Check Size Range", "slug": "check_size_range", "min": 1, "max": 2}],
    )

    config = await load_workspace_config(db_session)

    assert config.approved_sectors is None
    assert config.approved_geographies is None
