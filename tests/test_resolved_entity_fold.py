"""SIM-420: the fold and the matcher -- the judgment half of the resolved-entity
artifact. Hermetic: no DB and no network, so every case here is about what the
artifact DECIDES, never about where it is stored (that is
tests/test_resolved_entity_store.py).

The two failures this artifact exists to prevent are a common-name false
positive (an adapter corroborating a different company that shares the name)
and a false miss on a former name. Both are tested here directly, because
`conflicted` is sticky: a wrong match is not noise, it is unrecoverable
without a human.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.models.entity_resolution import EntityResolution
from app.models.resolved_entity import (
    REGISTRY_BC_REGISTRATION_NUMBER,
    REGISTRY_CIK,
    REGISTRY_ISED_CORPORATION_ID,
)
from app.services.entity_resolution.resolved import (
    DealEntity,
    fold_resolutions,
    normalize_name,
)

DEAL = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


def _row(
    *,
    source: str = "sec_edgar",
    status: str = "resolved",
    registry_id: str | None = "0000000042",
    legal_name: str | None = "Acme Inc.",
    former_names: list | None = None,
    created_at: dt.datetime | None = None,
    row_id: uuid.UUID | None = None,
) -> EntityResolution:
    """An unsaved entity_resolution row. The fold is a pure function of these
    attributes, so it can be exercised without a session."""
    return EntityResolution(
        id=row_id or uuid.uuid4(),
        org_id=1,
        deal_id=DEAL,
        source=source,
        status=status,
        query_name="Acme",
        registry_id=registry_id,
        legal_name=legal_name,
        former_names=former_names,
        matched_on="current_name" if status == "resolved" else None,
        evidence={},
        created_at=created_at or dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )


# --------------------------------------------------------------------------
# normalize_name: an equivalence, deliberately not a fuzzy match.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a,b",
    [
        ("Acme Inc.", "ACME INC"),
        ("Acme  Inc", "acme inc"),
        ("Acme, Inc.", "Acme Inc"),
        ("  Acme Inc  ", "Acme Inc"),
        ("Societe Generale", "Société Générale"),
        ("Québec Tech Inc.", "QUEBEC TECH INC"),
    ],
)
def test_registry_spelling_differences_normalize_to_the_same_name(a: str, b: str) -> None:
    assert normalize_name(a) == normalize_name(b)


@pytest.mark.parametrize(
    "a,b",
    [
        # Legal suffixes are NOT stripped: these are usually different
        # companies, and a match is what licenses an adapter to raise a
        # conflict.
        ("Acme Inc.", "Acme Holdings Inc."),
        ("Acme Inc.", "Acme Technologies Inc."),
        ("Acme Inc.", "Acme"),
    ],
)
def test_different_companies_do_not_normalize_together(a: str, b: str) -> None:
    assert normalize_name(a) != normalize_name(b)


def test_a_punctuation_only_name_normalizes_to_nothing() -> None:
    assert normalize_name("--- .,") == ""


# --------------------------------------------------------------------------
# fold_resolutions: no anchor -> no artifact. The no-signal contract.
# --------------------------------------------------------------------------


def test_no_rows_at_all_folds_to_none() -> None:
    assert fold_resolutions(DEAL, []) is None


def test_a_not_found_row_folds_to_none() -> None:
    """The common case in the target book: a pre-seed company no registry
    knows. It must produce NO artifact -- absence is never a conflict."""
    row = _row(status="not_found", registry_id=None, legal_name=None)
    assert fold_resolutions(DEAL, [row]) is None


def test_an_unresolved_row_folds_to_none() -> None:
    row = _row(status="unresolved", registry_id=None, legal_name=None)
    assert fold_resolutions(DEAL, [row]) is None


def test_a_source_with_no_registry_key_of_its_own_folds_to_none() -> None:
    """A resolved row from a registry this module has no cross-ref key for
    carries a name but no anchor. A name-only artifact looks like an anchor and
    is not one, so it is the same answer as nothing resolved."""
    row = _row(source="opencorporates", registry_id="X-1", legal_name="Acme Inc.")
    assert fold_resolutions(DEAL, [row]) is None


# --------------------------------------------------------------------------
# fold_resolutions: the artifact itself.
# --------------------------------------------------------------------------


def test_a_single_resolved_row_becomes_the_anchor() -> None:
    entity = fold_resolutions(DEAL, [_row()])

    assert entity is not None
    assert entity.deal_id == DEAL
    assert entity.canonical_name == "Acme Inc."
    assert entity.registry_id(REGISTRY_CIK) == "0000000042"
    assert entity.registry_id(REGISTRY_ISED_CORPORATION_ID) is None


def test_former_names_become_aliases() -> None:
    entity = fold_resolutions(
        DEAL,
        [
            _row(
                former_names=[
                    {"name": "Acme Holdings Ltd", "from": "1999-01-01", "to": "2005-02-02"}
                ]
            )
        ],
    )

    assert entity is not None
    assert entity.aliases == ("Acme Holdings Ltd",)
    assert entity.names == ("Acme Inc.", "Acme Holdings Ltd")


def test_registry_cross_refs_from_three_registries_land_in_one_artifact() -> None:
    """The whole point of the artifact: one deal, every anchor we hold."""
    entity = fold_resolutions(
        DEAL,
        [
            _row(source="sec_edgar", registry_id="0000000042", legal_name="Acme Inc."),
            _row(source="ised", registry_id="1234567", legal_name="ACME INC."),
            _row(source="orgbook_bc", registry_id="BC0999999", legal_name="Acme Inc"),
        ],
    )

    assert entity is not None
    assert entity.registry_id(REGISTRY_CIK) == "0000000042"
    assert entity.registry_id(REGISTRY_ISED_CORPORATION_ID) == "1234567"
    assert entity.registry_id(REGISTRY_BC_REGISTRATION_NUMBER) == "BC0999999"


def test_the_canadian_federal_register_wins_the_canonical_name() -> None:
    """Source precedence, not row order: for the target book (Canadian
    pre-seed) ISED's legal name is the one the deck means, and a SEC listing
    is usually a differently-named US subsidiary."""
    rows = [
        _row(source="sec_edgar", registry_id="0000000042", legal_name="Acme US Holdings Inc."),
        _row(source="ised", registry_id="1234567", legal_name="Acme Technologies Ltd."),
    ]

    assert fold_resolutions(DEAL, rows) is not None
    assert fold_resolutions(DEAL, rows).canonical_name == "Acme Technologies Ltd."  # type: ignore[union-attr]
    # And the same answer whichever order the rows arrive in.
    assert fold_resolutions(DEAL, list(reversed(rows))).canonical_name == (  # type: ignore[union-attr]
        "Acme Technologies Ltd."
    )


def test_a_second_registrys_different_legal_name_becomes_an_alias() -> None:
    entity = fold_resolutions(
        DEAL,
        [
            _row(source="ised", registry_id="1234567", legal_name="Acme Technologies Ltd."),
            _row(source="sec_edgar", registry_id="0000000042", legal_name="Acme US Holdings Inc."),
        ],
    )

    assert entity is not None
    assert entity.canonical_name == "Acme Technologies Ltd."
    assert entity.aliases == ("Acme US Holdings Inc.",)


def test_names_that_differ_only_in_spelling_are_one_alias_not_two() -> None:
    """Deduped on the normalized form, keeping the spelling a registry actually
    returned -- counting distinct raw strings would report two."""
    entity = fold_resolutions(
        DEAL,
        [
            _row(source="ised", registry_id="1234567", legal_name="Acme Inc."),
            _row(source="sec_edgar", registry_id="0000000042", legal_name="ACME, INC"),
        ],
    )

    assert entity is not None
    assert entity.canonical_name == "Acme Inc."
    assert entity.aliases == ()


def test_the_canonical_name_is_never_repeated_as_an_alias() -> None:
    entity = fold_resolutions(DEAL, [_row(former_names=[{"name": "ACME INC"}])])

    assert entity is not None
    assert entity.aliases == ()


def test_the_newest_attempt_from_a_registry_supplies_its_id() -> None:
    """An older, superseded lookup must never overwrite the one that replaced
    it."""
    old = _row(
        registry_id="0000000001",
        legal_name="Acme Inc.",
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    new = _row(
        registry_id="0000000042",
        legal_name="Acme Inc.",
        created_at=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
    )

    assert fold_resolutions(DEAL, [old, new]) is not None
    assert fold_resolutions(DEAL, [old, new]).registry_id(REGISTRY_CIK) == "0000000042"  # type: ignore[union-attr]
    assert fold_resolutions(DEAL, [new, old]).registry_id(REGISTRY_CIK) == "0000000042"  # type: ignore[union-attr]


def test_malformed_former_names_json_contributes_nothing_and_does_not_raise() -> None:
    """A row whose JSONB history is the wrong shape must not sink the fold for
    the whole deal."""
    entity = fold_resolutions(
        DEAL, [_row(former_names=["just a string", {"no_name_key": 1}, {"name": ""}, None])]
    )

    assert entity is not None
    assert entity.aliases == ()


def test_a_resolved_row_missing_its_legal_name_is_ignored() -> None:
    """entity_resolution allows a resolved row with a NULL legal_name; there is
    no name to canonicalize from it, so it contributes nothing rather than an
    empty canonical name."""
    assert fold_resolutions(DEAL, [_row(legal_name=None)]) is None


def test_only_resolved_rows_contribute_when_mixed_with_misses() -> None:
    entity = fold_resolutions(
        DEAL,
        [
            _row(source="sec_edgar", status="not_found", registry_id=None, legal_name=None),
            _row(source="ised", registry_id="1234567", legal_name="Acme Technologies Ltd."),
        ],
    )

    assert entity is not None
    assert entity.canonical_name == "Acme Technologies Ltd."
    assert entity.registry_id(REGISTRY_CIK) is None


# --------------------------------------------------------------------------
# DealEntity.matches: the name check adapters gate on.
# --------------------------------------------------------------------------


def _entity() -> DealEntity:
    return DealEntity(
        deal_id=DEAL,
        canonical_name="Acme Technologies Ltd.",
        aliases=("Acme Holdings Ltd", "Acme Labs"),
        registry_ids={REGISTRY_CIK: "0000000042"},
    )


def test_matches_the_canonical_name_through_spelling_differences() -> None:
    assert _entity().matches("ACME TECHNOLOGIES LTD") == "Acme Technologies Ltd."


def test_matches_a_former_name_and_reports_which_one() -> None:
    """The false-miss case: an older deck still uses the pre-rename name. The
    matched name is returned, not True, so the adapter can record that it was a
    former name rather than the current one."""
    assert _entity().matches("acme holdings ltd") == "Acme Holdings Ltd"


def test_a_different_company_sharing_a_word_does_not_match() -> None:
    """The common-name false positive this artifact exists to prevent."""
    assert _entity().matches("Acme Inc.") is None
    assert _entity().matches("Acme") is None


@pytest.mark.parametrize("candidate", [None, "", "   ", ".,-"])
def test_an_empty_or_punctuation_only_candidate_never_matches(candidate: str | None) -> None:
    """Otherwise a registry row with a blank name would match every entity."""
    assert _entity().matches(candidate) is None


def test_canonical_wins_when_a_name_is_both_canonical_and_an_alias() -> None:
    entity = DealEntity(
        deal_id=DEAL,
        canonical_name="Acme Inc.",
        aliases=("ACME INC",),
        registry_ids={REGISTRY_CIK: "0000000042"},
    )

    assert entity.matches("acme inc") == "Acme Inc."


# --------------------------------------------------------------------------
# DealEntity.registry_id: a typo must fail, not read as "no signal".
# --------------------------------------------------------------------------


def test_an_unknown_registry_key_raises_rather_than_returning_none() -> None:
    """Returning None would silently disable whichever adapter made the typo --
    it looks exactly like "this registry has no id for the company"."""
    with pytest.raises(ValueError, match="unknown registry"):
        _entity().registry_id("companies_house")


def test_a_known_registry_with_no_id_returns_none() -> None:
    assert _entity().registry_id(REGISTRY_ISED_CORPORATION_ID) is None


def test_to_json_round_trips_the_artifact() -> None:
    assert _entity().to_json() == {
        "deal_id": str(DEAL),
        "canonical_name": "Acme Technologies Ltd.",
        "aliases": ["Acme Holdings Ltd", "Acme Labs"],
        "registry_ids": {REGISTRY_CIK: "0000000042"},
    }
