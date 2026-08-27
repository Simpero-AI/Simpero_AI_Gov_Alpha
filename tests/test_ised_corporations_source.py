"""Hermetic tests for the ISED / Corporations Canada corroboration source and
its OrgBook BC fallthrough (SIM-421). No network and no DB: `fetch` is
injected, and the resolved entity is stubbed onto the session cache so
`check()` never issues a query.

The cases are grouped by the failure each one prevents. Most of this adapter's
value is in what it REFUSES to say -- `conflicted` is sticky, so a verdict it
should not have reached cannot be walked back without a human.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.models.claim import Claim
from app.models.resolved_entity import (
    REGISTRY_BC_REGISTRATION_NUMBER,
    REGISTRY_CIK,
    REGISTRY_ISED_CORPORATION_ID,
)
from app.services.corroboration import CorroborationVerdict
from app.services.corroboration_sources.ised_corporations import (
    FACT_HQ_PROVINCE,
    FACT_INCORPORATION_YEAR,
    FACT_JURISDICTION,
    FACT_STATUS,
    REGISTRY_ISED,
    REGISTRY_ORGBOOK_BC,
    IsedCorporationsSource,
)
from app.services.entity_resolution.resolved import _CACHE_KEY, DealEntity

DEAL = uuid.UUID("00000000-0000-0000-0000-0000000000bb")

_ISED_DETAIL_PREFIX = "https://ised-isde.canada.ca/cc/lgcy/api/corporations/"
_ISED_SEARCH_PREFIX = "https://ised-isde.canada.ca/cc/lgcy/api/corporations.json"
_ORGBOOK_AUTOCOMPLETE_PREFIX = "https://orgbook.gov.bc.ca/api/v4/search/autocomplete"
_ORGBOOK_TOPIC_PREFIX = "https://orgbook.gov.bc.ca/api/v4/topic/"


class _FakeSession:
    """Just enough AsyncSession for `load_resolved_entity`: an `info` dict.
    Pre-seeding the cache is what keeps these tests hermetic -- the loader
    returns the stub without ever reaching the repo."""

    def __init__(self, entity: DealEntity | None) -> None:
        self.info: dict[str, Any] = {_CACHE_KEY: {DEAL: entity}}


def _entity(
    canonical: str = "Acme Technologies Ltd.",
    aliases: tuple[str, ...] = ("Acme Holdings Ltd",),
    registry_ids: dict[str, str] | None = None,
) -> DealEntity:
    return DealEntity(
        deal_id=DEAL,
        canonical_name=canonical,
        aliases=aliases,
        registry_ids=registry_ids if registry_ids is not None else {REGISTRY_CIK: "0000000042"},
    )


def _claim(
    attribute: str = "Incorporated",
    raw: str | None = "2019",
    entity: str = "Acme Technologies Ltd.",
    attribute_raw: str | None = None,
) -> Claim:
    return Claim(
        deal_id=DEAL,
        entity=entity,
        attribute=attribute,
        attribute_raw=attribute_raw,
        claim_kind="qualitative",
        value={"raw": raw, "value_type": "text", "normalized": None},
    )


def _ised_record(
    *,
    corporation_id: str = "1234567",
    name: str = "ACME TECHNOLOGIES LTD.",
    incorporated: str | None = "2019-05-14",
    status: Any = None,
    province: str | None = "BC",
    annual_returns: Any = None,
) -> dict:
    record: dict[str, Any] = {
        "corporationId": corporation_id,
        "names": [{"name": name, "type": "current"}],
        "dateOfIncorporation": incorporated,
        "status": status if status is not None else [{"code": "ACT", "label": "Active"}],
    }
    if province is not None:
        record["registeredOffice"] = {"city": "Vancouver", "province": province, "country": "CA"}
    if annual_returns is not None:
        record["annualReturns"] = annual_returns
    return record


def _ised_search(*rows: dict) -> dict:
    return {"results": list(rows)}


def _orgbook_topic(
    *,
    source_id: str = "BC0999999",
    name: str = "ACME TECHNOLOGIES LTD.",
    registration_date: str | None = "2019-05-14",
    entity_status: str | None = "ACT",
    entity_type: str | None = "BC",
) -> dict:
    attributes = [
        {"type": "registration_date", "value": registration_date},
        {"type": "entity_type", "value": entity_type},
    ]
    if entity_status is not None:
        attributes.append({"type": "entity_status", "value": entity_status})
    return {
        "source_id": source_id,
        "names": [{"text": name}],
        "attributes": [a for a in attributes if a["value"] is not None],
    }


# Sentinel so a test can pass `ised_search=None` (a register that answered with
# nothing parseable) distinctly from not caring, which gets the matching search
# response most comparison tests need to reach a record at all.
_DEFAULT = object()

_MATCHING_SEARCH: dict = {
    "results": [{"corporationId": "1234567", "name": "ACME TECHNOLOGIES LTD."}]
}


def _fetch(
    *,
    ised_detail: Any = None,
    ised_search: Any = _DEFAULT,
    orgbook_autocomplete: Any = None,
    orgbook_topic: Any = None,
    fail: tuple[str, ...] = (),
    seen: list[str] | None = None,
):
    search = _MATCHING_SEARCH if ised_search is _DEFAULT else ised_search

    async def fetch(url: str) -> Any:
        if seen is not None:
            seen.append(url)
        for prefix in fail:
            if url.startswith(prefix):
                raise RuntimeError("boom")
        if url.startswith(_ISED_SEARCH_PREFIX):
            return search
        if url.startswith(_ISED_DETAIL_PREFIX):
            return ised_detail
        if url.startswith(_ORGBOOK_AUTOCOMPLETE_PREFIX):
            return orgbook_autocomplete
        if url.startswith(_ORGBOOK_TOPIC_PREFIX):
            return orgbook_topic
        raise AssertionError(f"unexpected url {url}")

    return fetch


# --------------------------------------------------------------------------
# Scope: claims this register cannot speak to.
# --------------------------------------------------------------------------


async def test_a_financial_claim_is_no_signal() -> None:
    """A corporate register holds no revenue figure. Comparing one would be
    comparing nothing."""
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record()))
    assert (
        await src.check(_FakeSession(_entity()), _claim(attribute="revenue", raw="1200000")) is None
    )


async def test_an_unrecognised_label_is_no_signal() -> None:
    """The label vocabulary is closed on purpose: guessing which registry fact
    an unfamiliar label meant would compare the wrong two things."""
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record()))
    assert await src.check(_FakeSession(_entity()), _claim(attribute="Team size")) is None


async def test_the_decks_own_label_is_read_from_attribute_raw() -> None:
    """Canonicalization files an entity claim under a catch-all, so the words
    the deck used survive only in attribute_raw."""
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record(incorporated="2019-05-14")))
    claim = _claim(attribute="core_unmapped", attribute_raw="Date of Incorporation", raw="2019")
    verdict = await src.check(_FakeSession(_entity()), claim)
    assert verdict is not None and verdict.agrees is True


# --------------------------------------------------------------------------
# Scope: whose company the claim is about.
# --------------------------------------------------------------------------


async def test_no_resolved_entity_is_no_signal() -> None:
    """Nothing resolved this deal, so there is no company to look up. This is
    the common case in the target book and must never read as a conflict."""
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record()))
    assert await src.check(_FakeSession(None), _claim()) is None


async def test_a_claim_about_a_different_company_is_no_signal() -> None:
    """A deck names customers and competitors too. Checking a customer's
    incorporation against the DEAL company's registration would be a conflict
    about the wrong entity entirely."""
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record()))
    claim = _claim(entity="Some Customer Inc.", raw="1994")
    assert await src.check(_FakeSession(_entity()), claim) is None


async def test_a_claim_naming_a_former_name_is_still_this_company() -> None:
    """The false-miss case: an older deck uses the pre-rename name."""
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record()))
    claim = _claim(entity="ACME HOLDINGS LTD", raw="2019")
    verdict = await src.check(_FakeSession(_entity()), claim)
    assert verdict is not None and verdict.agrees is True


# --------------------------------------------------------------------------
# Federal lookup and matching.
# --------------------------------------------------------------------------


async def test_a_pre_resolved_corporation_id_is_fetched_directly() -> None:
    seen: list[str] = []
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record(), seen=seen))
    entity = _entity(registry_ids={REGISTRY_ISED_CORPORATION_ID: "1234567"})

    verdict = await src.check(_FakeSession(entity), _claim())

    assert verdict is not None and verdict.agrees is True
    assert seen == [f"{_ISED_DETAIL_PREFIX}1234567.json"]  # no search round-trip


async def test_without_an_id_the_company_is_found_by_name_search() -> None:
    src = IsedCorporationsSource(
        fetch=_fetch(
            ised_search=_ised_search(
                {"corporationId": "1234567", "name": "ACME TECHNOLOGIES LTD."}
            ),
            ised_detail=_ised_record(),
        )
    )

    verdict = await src.check(_FakeSession(_entity()), _claim())

    assert verdict is not None
    assert verdict.result["registry"] == REGISTRY_ISED
    assert verdict.result["registry_id"] == "1234567"


async def test_a_search_hit_whose_name_is_not_ours_is_not_a_match() -> None:
    """The common-name false positive: the register answers with a different
    corporation that merely shares a word."""
    src = IsedCorporationsSource(
        fetch=_fetch(
            ised_search=_ised_search({"corporationId": "7654321", "name": "ACME HOLDINGS INC."}),
            ised_detail=_ised_record(),
            orgbook_autocomplete={"results": []},
        )
    )
    assert await src.check(_FakeSession(_entity()), _claim()) is None


async def test_two_different_corporations_under_one_name_are_ambiguous() -> None:
    src = IsedCorporationsSource(
        fetch=_fetch(
            ised_search=_ised_search(
                {"corporationId": "1111111", "name": "ACME TECHNOLOGIES LTD."},
                {"corporationId": "2222222", "name": "Acme Technologies Ltd"},
            ),
            ised_detail=_ised_record(),
            orgbook_autocomplete={"results": []},
        )
    )
    assert await src.check(_FakeSession(_entity()), _claim()) is None


async def test_one_corporation_listed_under_two_of_its_own_names_is_not_ambiguous() -> None:
    """Distinct ids, not row count. A register legitimately returns one company
    once per name it has held; counting rows would throw away a real answer."""
    src = IsedCorporationsSource(
        fetch=_fetch(
            ised_search=_ised_search(
                {"corporationId": "1234567", "name": "ACME TECHNOLOGIES LTD."},
                {"corporationId": "1234567", "name": "Acme Holdings Ltd"},
            ),
            ised_detail=_ised_record(),
        )
    )
    verdict = await src.check(_FakeSession(_entity()), _claim())
    assert verdict is not None and verdict.result["registry_id"] == "1234567"


async def test_a_stale_corporation_id_pointing_at_another_company_is_no_signal() -> None:
    """The name is re-checked even on an id fetch: a wrong id must surface as
    no-signal, not as a comparison against someone else's corporation."""
    src = IsedCorporationsSource(
        fetch=_fetch(
            ised_detail=_ised_record(name="COMPLETELY DIFFERENT CORP"),
            orgbook_autocomplete={"results": []},
        )
    )
    entity = _entity(registry_ids={REGISTRY_ISED_CORPORATION_ID: "1234567"})
    assert await src.check(_FakeSession(entity), _claim()) is None


# --------------------------------------------------------------------------
# The comparisons.
# --------------------------------------------------------------------------


async def test_matching_incorporation_year_agrees() -> None:
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record(incorporated="2019-05-14")))
    verdict = await src.check(_FakeSession(_entity()), _claim(raw="2019"))

    assert isinstance(verdict, CorroborationVerdict)
    assert verdict.agrees is True
    assert verdict.result["fact"] == FACT_INCORPORATION_YEAR
    assert verdict.result["registry_value"] == 2019


async def test_a_wrong_incorporation_year_disagrees() -> None:
    """A claim that unambiguously names the INCORPORATION year -- no founding
    language, in the label or the value -- and disagrees with the register is a
    genuine conflict."""
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record(incorporated="2019-05-14")))
    claim = _claim(attribute="Date of incorporation", raw="2015")
    verdict = await src.check(_FakeSession(_entity()), claim)

    assert verdict is not None and verdict.agrees is False
    assert verdict.result["registry_value"] == 2019


async def test_a_founding_year_that_predates_incorporation_is_no_signal() -> None:
    """ "Founded 2015, incorporated 2019" is an ordinary true statement: a company
    routinely operates before it incorporates, so a founding year that differs
    from the incorporation year is declined rather than flagged. The founding
    sense is read from the value's wording ("Founded") even under an
    "Incorporated" label, and from a founding label directly."""
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record(incorporated="2019-05-14")))
    assert await src.check(_FakeSession(_entity()), _claim(raw="Founded 2015")) is None
    founding = _claim(attribute="Year founded", raw="2015")
    assert await src.check(_FakeSession(_entity()), founding) is None


async def test_a_year_range_in_the_claim_is_no_signal() -> None:
    """ "2019-2021" names two years and is therefore no year at all."""
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record()))
    assert await src.check(_FakeSession(_entity()), _claim(raw="2019-2021")) is None


async def test_a_registry_record_with_no_incorporation_date_is_no_signal() -> None:
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record(incorporated=None)))
    assert await src.check(_FakeSession(_entity()), _claim(raw="2019")) is None


async def test_a_dissolved_company_the_deck_calls_active_disagrees() -> None:
    """The finding this adapter exists for: a register saying the company no
    longer exists while the deck says it is operating."""
    src = IsedCorporationsSource(
        fetch=_fetch(ised_detail=_ised_record(status=[{"code": "DIS", "label": "Dissolved"}]))
    )
    claim = _claim(attribute="Corporate status", raw="Active and in good standing")

    verdict = await src.check(_FakeSession(_entity()), claim)

    assert verdict is not None and verdict.agrees is False
    assert verdict.result["fact"] == FACT_STATUS
    assert verdict.result["registry_value"] == "dissolved"


async def test_an_active_company_the_deck_calls_active_agrees() -> None:
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record()))
    claim = _claim(attribute="Corporate status", raw="Active")

    verdict = await src.check(_FakeSession(_entity()), claim)

    assert verdict is not None and verdict.agrees is True


async def test_an_unmapped_registry_status_code_is_no_signal() -> None:
    """ "This company is dissolved" is a serious finding and must never be
    manufactured out of a code nobody verified."""
    src = IsedCorporationsSource(
        fetch=_fetch(ised_detail=_ised_record(status=[{"code": "XYZ", "label": "Something"}]))
    )
    claim = _claim(attribute="Corporate status", raw="Active")
    assert await src.check(_FakeSession(_entity()), claim) is None


async def test_a_claim_asserting_both_statuses_is_no_signal() -> None:
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record()))
    claim = _claim(attribute="Corporate status", raw="Active; predecessor dissolved 2016")
    assert await src.check(_FakeSession(_entity()), claim) is None


async def test_a_foreign_jurisdiction_claim_disagrees_with_a_federal_registration() -> None:
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record()))
    claim = _claim(attribute="Jurisdiction of incorporation", raw="Delaware")

    verdict = await src.check(_FakeSession(_entity()), claim)

    assert verdict is not None and verdict.agrees is False
    assert verdict.result["fact"] == FACT_JURISDICTION


async def test_a_coarser_jurisdiction_claim_agrees() -> None:
    """A federally registered company IS in Canada -- a deck saying "Canada" is
    right, not vague-and-therefore-wrong."""
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record()))
    claim = _claim(attribute="Jurisdiction", raw="Canada")

    verdict = await src.check(_FakeSession(_entity()), claim)

    assert verdict is not None and verdict.agrees is True


async def test_a_claim_naming_two_places_is_no_signal() -> None:
    """ "Vancouver office, Delaware holdco" is a coherent sentence about two
    real places; picking one to compare would invent a disagreement."""
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record()))
    claim = _claim(attribute="Jurisdiction", raw="Delaware holdco, British Columbia opco")
    assert await src.check(_FakeSession(_entity()), claim) is None


async def test_an_operating_hq_province_mismatch_is_no_signal() -> None:
    """An operating head office in a different province from the registered
    office is ordinary -- the registered office is often a law firm's -- so the
    difference is declined rather than flagged as a conflict."""
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record(province="BC")))
    claim = _claim(attribute="Head office", raw="Toronto, Ontario")
    assert await src.check(_FakeSession(_entity()), claim) is None


async def test_a_registered_office_province_mismatch_disagrees() -> None:
    """The one HQ label that hard-compares: a deck stating the legal REGISTERED
    office is in a province the register contradicts is a genuine conflict."""
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record(province="BC")))
    claim = _claim(attribute="Registered office", raw="Toronto, Ontario")

    verdict = await src.check(_FakeSession(_entity()), claim)

    assert verdict is not None and verdict.agrees is False
    assert verdict.result["fact"] == FACT_HQ_PROVINCE
    assert verdict.result["registry_value"] == "BC"


async def test_a_matching_hq_province_agrees() -> None:
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record(province="BC")))
    claim = _claim(attribute="Head office", raw="Vancouver, British Columbia")

    verdict = await src.check(_FakeSession(_entity()), claim)

    assert verdict is not None and verdict.agrees is True


async def test_a_us_headquarters_is_no_signal_not_a_conflict() -> None:
    """A registered office is not the operating headquarters, and a Canadian
    company with a US office is ordinary."""
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record(province="BC")))
    claim = _claim(attribute="Headquarters", raw="San Francisco, California")
    assert await src.check(_FakeSession(_entity()), claim) is None


async def test_a_province_name_inside_a_city_name_is_not_a_province() -> None:
    """Token matching, not substring: "on" is nothing at all inside "Toronto"."""
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record(province="ON")))
    claim = _claim(attribute="Head office", raw="Toronto")
    assert await src.check(_FakeSession(_entity()), claim) is None


async def test_a_two_letter_code_as_a_preposition_is_not_a_province() -> None:
    """ "on" standing in prose is the English preposition, not Ontario -- an
    ordinary Vancouver address must not read as an Ontario-vs-BC conflict. Even
    under the registered-office label, which does hard-compare, no province is
    found in "on"."""
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record(province="BC")))
    claim = _claim(attribute="Registered office", raw="Located on Granville Street")
    assert await src.check(_FakeSession(_entity()), claim) is None


async def test_a_comma_form_two_letter_province_code_is_recognized() -> None:
    """A two-letter code IS a province in the register's "City, BC" comma form,
    so real registered-office data still corroborates."""
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record(province="BC")))
    claim = _claim(attribute="Registered office", raw="Vancouver, BC")

    verdict = await src.check(_FakeSession(_entity()), claim)

    assert verdict is not None and verdict.agrees is True


async def test_good_standing_is_reported_but_never_drives_the_verdict() -> None:
    """A late annual return is filing housekeeping, not something the deck
    claimed -- it must not mark a deal conflicted."""
    src = IsedCorporationsSource(
        fetch=_fetch(
            ised_detail=_ised_record(
                incorporated="2019-05-14", annual_returns=[{"year": 2025, "filed": False}]
            )
        )
    )

    verdict = await src.check(_FakeSession(_entity()), _claim(raw="2019"))

    assert verdict is not None
    assert verdict.agrees is True
    assert verdict.result["annual_returns_current"] is False


# --------------------------------------------------------------------------
# The OrgBook BC fallthrough.
# --------------------------------------------------------------------------


async def test_a_federal_miss_falls_through_to_orgbook_bc() -> None:
    """The normal case for the target book: most Canadian startups incorporate
    provincially and never appear federally at all."""
    src = IsedCorporationsSource(
        fetch=_fetch(
            ised_search=_ised_search(),
            orgbook_autocomplete={
                "results": [{"value": "ACME TECHNOLOGIES LTD.", "topic_source_id": "BC0999999"}]
            },
            orgbook_topic=_orgbook_topic(),
        )
    )

    verdict = await src.check(_FakeSession(_entity()), _claim(raw="2019"))

    assert verdict is not None
    assert verdict.agrees is True
    assert verdict.result["registry"] == REGISTRY_ORGBOOK_BC
    assert verdict.result["registry_id"] == "BC0999999"


async def test_a_pre_resolved_bc_registration_number_skips_autocomplete() -> None:
    seen: list[str] = []
    src = IsedCorporationsSource(
        fetch=_fetch(ised_search=_ised_search(), orgbook_topic=_orgbook_topic(), seen=seen)
    )
    entity = _entity(registry_ids={REGISTRY_BC_REGISTRATION_NUMBER: "BC0999999"})

    verdict = await src.check(_FakeSession(entity), _claim(raw="2019"))

    assert verdict is not None and verdict.result["registry"] == REGISTRY_ORGBOOK_BC
    assert not any(u.startswith(_ORGBOOK_AUTOCOMPLETE_PREFIX) for u in seen)


async def test_a_bc_registration_disagrees_with_an_ontario_jurisdiction_claim() -> None:
    src = IsedCorporationsSource(
        fetch=_fetch(
            ised_search=_ised_search(),
            orgbook_autocomplete={
                "results": [{"value": "ACME TECHNOLOGIES LTD.", "topic_source_id": "BC0999999"}]
            },
            orgbook_topic=_orgbook_topic(),
        )
    )
    claim = _claim(attribute="Jurisdiction", raw="Ontario")

    verdict = await src.check(_FakeSession(_entity()), claim)

    assert verdict is not None and verdict.agrees is False


async def test_an_extraprovincial_bc_registration_asserts_no_jurisdiction() -> None:
    """A BC registry entry can be an extraprovincial registration of a company
    incorporated elsewhere. Reading that as a BC incorporation would contradict
    a perfectly accurate deck."""
    src = IsedCorporationsSource(
        fetch=_fetch(
            ised_search=_ised_search(),
            orgbook_autocomplete={
                "results": [{"value": "ACME TECHNOLOGIES LTD.", "topic_source_id": "BC0999999"}]
            },
            orgbook_topic=_orgbook_topic(entity_type="XP"),
        )
    )
    claim = _claim(attribute="Jurisdiction", raw="Ontario")
    assert await src.check(_FakeSession(_entity()), claim) is None


async def test_orgbook_never_answers_an_hq_question() -> None:
    """A BC registration says nothing reliable about where the registered
    office is, so an ordinary head office elsewhere must not become a location
    conflict."""
    src = IsedCorporationsSource(
        fetch=_fetch(
            ised_search=_ised_search(),
            orgbook_autocomplete={
                "results": [{"value": "ACME TECHNOLOGIES LTD.", "topic_source_id": "BC0999999"}]
            },
            orgbook_topic=_orgbook_topic(),
        )
    )
    claim = _claim(attribute="Head office", raw="Toronto, Ontario")
    assert await src.check(_FakeSession(_entity()), claim) is None


async def test_orgbook_historical_status_is_not_read_as_dissolved() -> None:
    """ "HIS" marks a superseded credential, not a dissolved company. Treating
    it as dissolution would be a false and serious finding."""
    src = IsedCorporationsSource(
        fetch=_fetch(
            ised_search=_ised_search(),
            orgbook_autocomplete={
                "results": [{"value": "ACME TECHNOLOGIES LTD.", "topic_source_id": "BC0999999"}]
            },
            orgbook_topic=_orgbook_topic(entity_status="HIS"),
        )
    )
    claim = _claim(attribute="Corporate status", raw="Active")
    assert await src.check(_FakeSession(_entity()), claim) is None


async def test_both_registers_missing_is_no_signal() -> None:
    """Never-checked is not a conflict."""
    src = IsedCorporationsSource(
        fetch=_fetch(ised_search=_ised_search(), orgbook_autocomplete={"results": []})
    )
    assert await src.check(_FakeSession(_entity()), _claim()) is None


async def test_an_ambiguous_bc_autocomplete_is_no_signal() -> None:
    src = IsedCorporationsSource(
        fetch=_fetch(
            ised_search=_ised_search(),
            orgbook_autocomplete={
                "results": [
                    {"value": "ACME TECHNOLOGIES LTD.", "topic_source_id": "BC0000001"},
                    {"value": "Acme Technologies Ltd", "topic_source_id": "BC0000002"},
                ]
            },
        )
    )
    assert await src.check(_FakeSession(_entity()), _claim()) is None


# --------------------------------------------------------------------------
# Failure handling.
# --------------------------------------------------------------------------


async def test_an_unreachable_federal_register_still_lets_bc_answer() -> None:
    """A transport failure is never-checked, and must not stop the other
    register from answering."""
    src = IsedCorporationsSource(
        fetch=_fetch(
            orgbook_autocomplete={
                "results": [{"value": "ACME TECHNOLOGIES LTD.", "topic_source_id": "BC0999999"}]
            },
            orgbook_topic=_orgbook_topic(),
            fail=(_ISED_SEARCH_PREFIX, _ISED_DETAIL_PREFIX),
        )
    )

    verdict = await src.check(_FakeSession(_entity()), _claim(raw="2019"))

    assert verdict is not None and verdict.result["registry"] == REGISTRY_ORGBOOK_BC


async def test_both_registers_unreachable_is_no_signal_never_a_conflict() -> None:
    src = IsedCorporationsSource(
        fetch=_fetch(
            fail=(
                _ISED_SEARCH_PREFIX,
                _ISED_DETAIL_PREFIX,
                _ORGBOOK_AUTOCOMPLETE_PREFIX,
                _ORGBOOK_TOPIC_PREFIX,
            )
        )
    )
    assert await src.check(_FakeSession(_entity()), _claim()) is None


@pytest.mark.parametrize(
    "payload",
    [None, {}, [], "not json", {"results": "nope"}, {"corporationId": None}],
)
async def test_a_malformed_registry_payload_is_no_signal(payload: Any) -> None:
    """An unexpected shape must yield no-signal at every step, never a wrong
    verdict."""
    src = IsedCorporationsSource(
        fetch=_fetch(ised_search=payload, ised_detail=payload, orgbook_autocomplete=payload)
    )
    assert await src.check(_FakeSession(_entity()), _claim()) is None


async def test_a_claim_with_no_readable_value_is_no_signal() -> None:
    src = IsedCorporationsSource(fetch=_fetch(ised_detail=_ised_record()))
    assert await src.check(_FakeSession(_entity()), _claim(raw=None)) is None


def test_the_source_is_not_registered_yet() -> None:
    """Registration attaches once SIM-416 settles the corroboration pass's I/O
    placement -- until then a network call must not sit inside the verify
    transaction."""
    from app.services.corroboration import CORROBORATION_SOURCES

    assert not any(
        getattr(s, "name", None) == IsedCorporationsSource.name for s in CORROBORATION_SOURCES
    )
