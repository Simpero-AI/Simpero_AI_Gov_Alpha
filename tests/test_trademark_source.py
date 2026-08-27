"""Hermetic tests for the CIPO / USPTO trademark corroboration source
(SIM-431). No network and no DB: `fetch` is injected, and the resolved entity
is stubbed onto the session cache so `check()` never issues a query.

Two of these findings are among the most damaging this pipeline could get
wrong -- telling a partner their brand belongs to someone else, or that their
stated first use is false. So most of the file is about the cases where the
source must stay silent: a mark that is not registered, a mark whose text
does not match, an owner line the entity cannot be matched against.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.models.claim import Claim
from app.models.resolved_entity import REGISTRY_CIK
from app.services.corroboration import CorroborationVerdict
from app.services.corroboration_sources.trademarks import (
    FACT_TRADEMARK_FIRST_USE,
    FACT_TRADEMARK_OWNER,
    REGISTRY_CIPO,
    REGISTRY_USPTO,
    TrademarkSource,
)
from app.services.entity_resolution.resolved import _CACHE_KEY, DealEntity

DEAL = uuid.UUID("00000000-0000-0000-0000-0000000000cc")

_CIPO_PREFIX = "https://api.ic.gc.ca/opic-cipo/trademarks/v1/marks"
_USPTO_PREFIX = "https://developer.uspto.gov/ds-api/trademarks/v1/records"


class _FakeSession:
    """Just enough AsyncSession for `load_resolved_entity`: an `info` dict."""

    def __init__(self, entity: DealEntity | None) -> None:
        self.info: dict[str, Any] = {_CACHE_KEY: {DEAL: entity}}


def _entity(
    canonical: str = "Acme Technologies Ltd.",
    aliases: tuple[str, ...] = ("Acme Holdings Ltd",),
) -> DealEntity:
    return DealEntity(
        deal_id=DEAL,
        canonical_name=canonical,
        aliases=aliases,
        registry_ids={REGISTRY_CIK: "0000000042"},
    )


def _claim(
    *,
    attribute: str = "Trademark",
    raw: str | None = "The ACME® mark is registered and owned by the company",
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


def _cipo(
    *,
    registration_number: str = "TMA123456",
    mark_text: str = "ACME",
    status: str = "Registered",
    owner: Any = "ACME TECHNOLOGIES LTD.",
    first_use: str | None = "2015-03-01",
) -> dict:
    return {
        "registrationNumber": registration_number,
        "markText": mark_text,
        "status": status,
        "owners": [{"name": owner}] if isinstance(owner, str) else owner,
        "dateOfFirstUseInCanada": first_use,
    }


def _uspto(
    *,
    registration_number: str = "5555555",
    mark_text: str = "ACME",
    status: str = "Registered",
    owner: str = "Acme Technologies Ltd",
    first_use: str | None = "2015-03-01",
) -> dict:
    return {
        "registrationNumber": registration_number,
        "markIdentification": mark_text,
        "status": status,
        "owners": [{"name": owner}],
        "firstUseDate": first_use,
    }


def _fetch(
    *,
    cipo: Any = None,
    uspto: Any = None,
    fail: tuple[str, ...] = (),
    seen: list[str] | None = None,
):
    async def fetch(url: str) -> Any:
        if seen is not None:
            seen.append(url)
        for prefix in fail:
            if url.startswith(prefix):
                raise RuntimeError("boom")
        if url.startswith(_CIPO_PREFIX):
            return {"marks": list(cipo)} if cipo is not None else {"marks": []}
        if url.startswith(_USPTO_PREFIX):
            return {"results": list(uspto)} if uspto is not None else {"results": []}
        raise AssertionError(f"unexpected url {url}")

    return fetch


# --------------------------------------------------------------------------
# Scope.
# --------------------------------------------------------------------------


async def test_a_claim_that_is_not_about_a_brand_is_no_signal() -> None:
    src = TrademarkSource(fetch=_fetch(cipo=[_cipo()]))
    assert await src.check(_FakeSession(_entity()), _claim(attribute="revenue")) is None


async def test_no_resolved_entity_is_no_signal() -> None:
    """There is no owner to compare a register line against."""
    src = TrademarkSource(fetch=_fetch(cipo=[_cipo()]))
    assert await src.check(_FakeSession(None), _claim()) is None


async def test_another_companys_brand_named_in_the_deck_is_no_signal() -> None:
    """A deck names a partner's platform and a competitor's product. Comparing
    one of those against THIS deal's ownership would be a brand-ownership
    finding about the wrong company."""
    src = TrademarkSource(fetch=_fetch(cipo=[_cipo()]))
    assert await src.check(_FakeSession(_entity()), _claim(entity="Partner Corp")) is None


async def test_a_claim_with_no_readable_text_is_no_signal() -> None:
    src = TrademarkSource(fetch=_fetch(cipo=[_cipo()]))
    assert await src.check(_FakeSession(_entity()), _claim(raw=None)) is None


async def test_the_decks_own_label_is_read_from_attribute_raw() -> None:
    src = TrademarkSource(fetch=_fetch(cipo=[_cipo()]))
    claim = _claim(attribute="core_unmapped", attribute_raw="Registered trademark")
    verdict = await src.check(_FakeSession(_entity()), claim)
    assert verdict is not None and verdict.agrees is True


# --------------------------------------------------------------------------
# Which mark the claim is about.
# --------------------------------------------------------------------------


async def test_a_symbol_marked_brand_is_the_mark_searched_for() -> None:
    seen: list[str] = []
    src = TrademarkSource(fetch=_fetch(cipo=[_cipo(mark_text="ACME")], seen=seen))

    verdict = await src.check(_FakeSession(_entity()), _claim(raw="Our ACME® mark"))

    assert verdict is not None
    assert "ACME" in seen[0]


async def test_a_quoted_brand_is_the_mark_searched_for() -> None:
    src = TrademarkSource(fetch=_fetch(cipo=[_cipo(mark_text="Zephyr")]))
    claim = _claim(raw='Our brand "Zephyr" is a registered trademark')

    verdict = await src.check(_FakeSession(_entity()), claim)

    assert verdict is not None and verdict.result["mark_text"] == "Zephyr"


async def test_with_no_named_mark_the_companys_own_name_is_used() -> None:
    """The common pre-seed case: the company and the brand are the same word."""
    src = TrademarkSource(fetch=_fetch(cipo=[_cipo(mark_text="Acme Technologies Ltd.")]))
    claim = _claim(raw="Our trademark is registered")

    verdict = await src.check(_FakeSession(_entity()), claim)

    assert verdict is not None and verdict.result["mark_text"] == "Acme Technologies Ltd."


async def test_a_claim_naming_two_marks_is_no_signal() -> None:
    """No single thing to check; picking one would report a verdict about the
    other."""
    src = TrademarkSource(fetch=_fetch(cipo=[_cipo()]))
    claim = _claim(raw="Our ACME® and ZEPHYR® marks are both registered")
    assert await src.check(_FakeSession(_entity()), claim) is None


async def test_a_register_hit_whose_text_is_a_different_mark_is_not_a_match() -> None:
    src = TrademarkSource(fetch=_fetch(cipo=[_cipo(mark_text="ACME PLUS")]))
    assert await src.check(_FakeSession(_entity()), _claim(raw="Our ACME® mark")) is None


async def test_two_different_registrations_of_the_same_text_are_ambiguous() -> None:
    """Two owners would otherwise make the verdict depend on result order."""
    src = TrademarkSource(
        fetch=_fetch(
            cipo=[
                _cipo(registration_number="TMA1", owner="ACME TECHNOLOGIES LTD."),
                _cipo(registration_number="TMA2", owner="Someone Else Inc."),
            ]
        )
    )
    assert await src.check(_FakeSession(_entity()), _claim(raw="Our ACME® mark")) is None


# --------------------------------------------------------------------------
# Registered marks only.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["Pending", "Abandoned", "Expired", "Opposed", "Filed", ""])
async def test_an_unregistered_mark_is_no_signal(status: str) -> None:
    """An unregistered brand is completely ordinary -- most pre-seed companies
    have never filed."""
    src = TrademarkSource(fetch=_fetch(cipo=[_cipo(status=status)]))
    assert await src.check(_FakeSession(_entity()), _claim(raw="Our ACME® mark")) is None


async def test_no_mark_at_either_office_is_no_signal() -> None:
    src = TrademarkSource(fetch=_fetch())
    assert await src.check(_FakeSession(_entity()), _claim(raw="Our ACME® mark")) is None


# --------------------------------------------------------------------------
# Owner comparison.
# --------------------------------------------------------------------------


async def test_a_mark_registered_to_the_company_agrees() -> None:
    src = TrademarkSource(fetch=_fetch(cipo=[_cipo(owner="ACME TECHNOLOGIES LTD.")]))

    verdict = await src.check(_FakeSession(_entity()), _claim(raw="Our ACME® mark"))

    assert isinstance(verdict, CorroborationVerdict)
    assert verdict.agrees is True
    assert verdict.result["fact"] == FACT_TRADEMARK_OWNER
    assert verdict.result["registry"] == REGISTRY_CIPO
    assert verdict.result["registration_id"] == "TMA123456"


async def test_a_mark_registered_to_somebody_else_disagrees() -> None:
    """One of the two findings this source exists for."""
    src = TrademarkSource(fetch=_fetch(cipo=[_cipo(owner="Northwind Brands Inc.")]))

    verdict = await src.check(_FakeSession(_entity()), _claim(raw="Our ACME® mark"))

    assert verdict is not None and verdict.agrees is False
    assert verdict.result["registered_owner"] == "Northwind Brands Inc."
    assert verdict.result["claim_value"] == "Acme Technologies Ltd."


async def test_a_mark_still_registered_under_a_former_name_is_still_theirs() -> None:
    """The register lags a rename. Reading that as someone else's mark would be
    a false ownership conflict."""
    src = TrademarkSource(fetch=_fetch(cipo=[_cipo(owner="ACME HOLDINGS LTD")]))

    verdict = await src.check(_FakeSession(_entity()), _claim(raw="Our ACME® mark"))

    assert verdict is not None and verdict.agrees is True


async def test_a_record_with_no_owner_line_is_no_signal() -> None:
    src = TrademarkSource(fetch=_fetch(cipo=[_cipo(owner=[])]))
    assert await src.check(_FakeSession(_entity()), _claim(raw="Our ACME® mark")) is None


# --------------------------------------------------------------------------
# First-use comparison.
# --------------------------------------------------------------------------


async def test_a_matching_first_use_year_agrees() -> None:
    src = TrademarkSource(fetch=_fetch(cipo=[_cipo(first_use="2015-03-01")]))
    claim = _claim(attribute="First use in commerce", raw="ACME® in market since 2015")

    verdict = await src.check(_FakeSession(_entity()), claim)

    assert verdict is not None and verdict.agrees is True
    assert verdict.result["fact"] == FACT_TRADEMARK_FIRST_USE


async def test_a_first_use_year_later_than_the_deck_claims_disagrees() -> None:
    """The other finding this source exists for."""
    src = TrademarkSource(fetch=_fetch(cipo=[_cipo(first_use="2021-08-09")]))
    claim = _claim(attribute="First use in commerce", raw="ACME® in market since 2015")

    verdict = await src.check(_FakeSession(_entity()), claim)

    assert verdict is not None and verdict.agrees is False
    assert verdict.result["claim_value"] == 2015
    assert verdict.result["registry_value"] == 2021


async def test_a_first_use_claim_naming_no_year_is_no_signal() -> None:
    src = TrademarkSource(fetch=_fetch(cipo=[_cipo()]))
    claim = _claim(attribute="First use", raw="ACME® has been in market for years")
    assert await src.check(_FakeSession(_entity()), claim) is None


async def test_a_first_use_claim_naming_two_years_is_no_signal() -> None:
    src = TrademarkSource(fetch=_fetch(cipo=[_cipo()]))
    claim = _claim(attribute="First use", raw="ACME® used 2015-2018")
    assert await src.check(_FakeSession(_entity()), claim) is None


async def test_a_record_with_no_first_use_date_is_no_signal() -> None:
    src = TrademarkSource(fetch=_fetch(cipo=[_cipo(first_use=None)]))
    claim = _claim(attribute="First use", raw="ACME® in market since 2015")
    assert await src.check(_FakeSession(_entity()), claim) is None


async def test_a_first_use_label_wins_over_the_owner_label() -> None:
    """ "trademark first use" is in both vocabularies by intent; a claim about a
    DATE must be compared against a date."""
    src = TrademarkSource(fetch=_fetch(cipo=[_cipo(owner="Northwind Brands Inc.")]))
    claim = _claim(attribute="Trademark first use", raw="ACME® in market since 2015")

    verdict = await src.check(_FakeSession(_entity()), claim)

    assert verdict is not None and verdict.result["fact"] == FACT_TRADEMARK_FIRST_USE


# --------------------------------------------------------------------------
# The USPTO fallthrough.
# --------------------------------------------------------------------------


async def test_a_cipo_miss_falls_through_to_uspto() -> None:
    src = TrademarkSource(fetch=_fetch(uspto=[_uspto()]))

    verdict = await src.check(_FakeSession(_entity()), _claim(raw="Our ACME® mark"))

    assert verdict is not None
    assert verdict.result["registry"] == REGISTRY_USPTO
    assert verdict.result["registration_id"] == "5555555"


async def test_cipo_is_asked_first() -> None:
    seen: list[str] = []
    src = TrademarkSource(fetch=_fetch(cipo=[_cipo()], uspto=[_uspto()], seen=seen))

    await src.check(_FakeSession(_entity()), _claim(raw="Our ACME® mark"))

    assert len(seen) == 1
    assert seen[0].startswith(_CIPO_PREFIX)


async def test_an_unreachable_cipo_still_lets_uspto_answer() -> None:
    src = TrademarkSource(fetch=_fetch(uspto=[_uspto()], fail=(_CIPO_PREFIX,)))

    verdict = await src.check(_FakeSession(_entity()), _claim(raw="Our ACME® mark"))

    assert verdict is not None and verdict.result["registry"] == REGISTRY_USPTO


async def test_both_offices_unreachable_is_no_signal_never_a_conflict() -> None:
    src = TrademarkSource(fetch=_fetch(fail=(_CIPO_PREFIX, _USPTO_PREFIX)))
    assert await src.check(_FakeSession(_entity()), _claim(raw="Our ACME® mark")) is None


@pytest.mark.parametrize(
    "payload", [None, {}, [], "not json", {"marks": "nope"}, {"marks": [None, 1]}]
)
async def test_a_malformed_response_is_no_signal(payload: Any) -> None:
    async def fetch(url: str) -> Any:
        return payload

    assert (
        await TrademarkSource(fetch=fetch).check(
            _FakeSession(_entity()), _claim(raw="Our ACME® mark")
        )
        is None
    )


def test_the_source_is_not_registered_yet() -> None:
    """Registration attaches once SIM-416 settles the corroboration pass's I/O
    placement."""
    from app.services.corroboration import CORROBORATION_SOURCES

    assert not any(getattr(s, "name", None) == TrademarkSource.name for s in CORROBORATION_SOURCES)
