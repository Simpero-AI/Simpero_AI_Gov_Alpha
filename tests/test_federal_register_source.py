"""Hermetic tests for the US Federal Register corroboration source (SIM-422).
No network and no DB: `fetch` is injected, and check() never touches the
session.

This source is presence-only -- it can agree or say nothing, never disagree --
so most of these tests are about the cases where it must stay silent. The
common case for the target book (a Canadian pre-seed company with no US
federal footprint at all) is one of them.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.models.claim import Claim
from app.services.corroboration import CorroborationVerdict
from app.services.corroboration_sources.federal_register import (
    LANE_ENTITY,
    LANE_INSTRUMENT,
    FederalRegisterSource,
)


def _claim(
    *,
    attribute: str = "Regulatory clearance",
    raw: str | None = "Device cleared under 21 CFR 820",
    entity: str = "Northwind Diagnostics Inc.",
    claim_type: str = "regulatory",
    section: str | None = None,
) -> Claim:
    return Claim(
        entity=entity,
        attribute=attribute,
        section=section,
        claim_type=claim_type,
        claim_kind="qualitative",
        value={"raw": raw, "value_type": "text", "normalized": None},
    )


def _document(
    *,
    document_number: str = "2024-01234",
    title: str = "Medical Devices; Quality System Regulation Amendments",
    abstract: str = "The Food and Drug Administration is amending the requirements.",
    agencies: list[dict] | None = None,
    cfr_references: list[dict] | None = None,
    docket_ids: list[str] | None = None,
    citation: str = "89 FR 12345",
    doc_type: str = "Rule",
) -> dict:
    return {
        "document_number": document_number,
        "title": title,
        "abstract": abstract,
        "type": doc_type,
        "citation": citation,
        "publication_date": "2024-02-01",
        "html_url": f"https://www.federalregister.gov/d/{document_number}",
        "agencies": agencies
        if agencies is not None
        else [{"name": "Food and Drug Administration", "slug": "food-and-drug-administration"}],
        "cfr_references": cfr_references if cfr_references is not None else [],
        "docket_ids": docket_ids if docket_ids is not None else [],
    }


def _fetch(payload: Any = None, *, raises: bool = False, seen: list[str] | None = None):
    async def fetch(url: str) -> Any:
        if seen is not None:
            seen.append(url)
        if raises:
            raise RuntimeError("boom")
        return payload

    return fetch


def _results(*documents: dict) -> dict:
    return {"count": len(documents), "results": list(documents)}


# --------------------------------------------------------------------------
# Scope: what this source will not look at.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("claim_type", ["numerical", "temporal", "entity_attribute", "unknown"])
async def test_a_non_regulatory_claim_is_no_signal(claim_type: str) -> None:
    """Scope comes from the pipeline's own claim_type, not from re-deriving one
    out of the wording -- a claim nothing typed as regulatory is not regulatory
    just because it mentions an agency."""
    src = FederalRegisterSource(fetch=_fetch(_results(_document())))
    assert await src.check(None, _claim(claim_type=claim_type)) is None


async def test_a_vague_regulatory_claim_naming_nothing_is_no_signal() -> None:
    """Searching for "we comply with applicable regulations" returns thousands
    of notices, none of which corroborate anything."""
    src = FederalRegisterSource(fetch=_fetch(_results(_document())))
    claim = _claim(attribute="Compliance", raw="We comply with all applicable regulations")
    assert await src.check(None, claim) is None


async def test_an_empty_claim_is_no_signal() -> None:
    src = FederalRegisterSource(fetch=_fetch(_results(_document())))
    assert await src.check(None, _claim(attribute="", raw=None)) is None


async def test_a_claim_citing_two_different_rules_is_no_signal() -> None:
    """Two instruments give no single thing to confirm, and confirming one
    would overstate what was checked."""
    src = FederalRegisterSource(fetch=_fetch(_results(_document())))
    claim = _claim(raw="Subject to 21 CFR 820 and 47 CFR 15")
    assert await src.check(None, claim) is None


async def test_a_claim_naming_two_agencies_is_no_signal() -> None:
    src = FederalRegisterSource(fetch=_fetch(_results(_document())))
    claim = _claim(attribute="Approvals", raw="Cleared by the FDA and registered with the FTC")
    assert await src.check(None, claim) is None


# --------------------------------------------------------------------------
# The instrument lane.
# --------------------------------------------------------------------------


async def test_a_cited_cfr_part_the_register_carries_agrees() -> None:
    src = FederalRegisterSource(
        fetch=_fetch(_results(_document(cfr_references=[{"title": 21, "part": 820}])))
    )

    verdict = await src.check(None, _claim(raw="Device cleared under 21 CFR 820"))

    assert isinstance(verdict, CorroborationVerdict)
    assert verdict.agrees is True
    assert verdict.result["lane"] == LANE_INSTRUMENT
    assert verdict.result["instrument"] == "21 CFR 820"
    assert verdict.result["document"]["document_number"] == "2024-01234"


async def test_a_document_that_merely_mentions_the_rule_does_not_confirm_it() -> None:
    """The API's term search matches document bodies, so a rule that cites
    another rule in passing scores too. Confirming on that would corroborate
    the wrong document."""
    src = FederalRegisterSource(
        fetch=_fetch(_results(_document(cfr_references=[{"title": 40, "part": 60}])))
    )
    assert await src.check(None, _claim(raw="Device cleared under 21 CFR 820")) is None


async def test_a_cfr_citation_with_periods_is_still_recognised() -> None:
    src = FederalRegisterSource(
        fetch=_fetch(_results(_document(cfr_references=[{"title": 21, "part": 820}])))
    )
    verdict = await src.check(None, _claim(raw="Subject to 21 C.F.R. Part 820"))
    assert verdict is not None and verdict.result["instrument"] == "21 CFR 820"


async def test_a_federal_register_citation_confirms_the_document_itself() -> None:
    src = FederalRegisterSource(fetch=_fetch(_results(_document(citation="89 FR 12345"))))

    verdict = await src.check(None, _claim(raw="Published at 89 FR 12345"))

    assert verdict is not None and verdict.agrees is True
    assert verdict.result["instrument_kind"] == "fr_citation"


async def test_a_docket_id_is_confirmed_against_the_documents_own_dockets() -> None:
    src = FederalRegisterSource(
        fetch=_fetch(_results(_document(docket_ids=["FDA-2020-N-1234"], citation="89 FR 99999")))
    )

    verdict = await src.check(None, _claim(raw="See docket FDA-2020-N-1234"))

    assert verdict is not None and verdict.agrees is True
    assert verdict.result["instrument"] == "FDA-2020-N-1234"


async def test_a_docket_the_document_does_not_carry_is_no_signal() -> None:
    src = FederalRegisterSource(
        fetch=_fetch(_results(_document(docket_ids=["EPA-HQ-OAR-2021-0317"], citation="89 FR 1")))
    )
    assert await src.check(None, _claim(raw="See docket FDA-2020-N-1234")) is None


async def test_an_instrument_the_register_does_not_carry_is_no_signal_not_a_conflict() -> None:
    """The rule may be a state rule, a Canadian one, or older than the corpus
    the search reaches. Absence is never contradiction."""
    src = FederalRegisterSource(fetch=_fetch(_results()))
    assert await src.check(None, _claim(raw="Device cleared under 21 CFR 820")) is None


# --------------------------------------------------------------------------
# The entity-footprint lane.
# --------------------------------------------------------------------------


async def test_an_agency_document_naming_the_company_agrees() -> None:
    src = FederalRegisterSource(
        fetch=_fetch(
            _results(
                _document(
                    title="Northwind Diagnostics Inc.; Withdrawal of Approval",
                    abstract="Notice concerning the applicant.",
                )
            )
        )
    )
    claim = _claim(attribute="FDA enforcement", raw="Subject to an FDA enforcement notice")

    verdict = await src.check(None, claim)

    assert verdict is not None and verdict.agrees is True
    assert verdict.result["lane"] == LANE_ENTITY
    assert verdict.result["agency_slug"] == "food-and-drug-administration"


async def test_the_common_case_no_federal_footprint_is_no_signal() -> None:
    """A Canadian pre-seed company has no US federal footprint at all. That is
    not a finding, and this source must produce no false signal from it."""
    src = FederalRegisterSource(fetch=_fetch(_results()))
    claim = _claim(attribute="FDA clearance", raw="Pursuing FDA clearance")
    assert await src.check(None, claim) is None


async def test_a_document_from_the_agency_that_does_not_name_the_company_is_no_signal() -> None:
    src = FederalRegisterSource(fetch=_fetch(_results(_document())))
    claim = _claim(attribute="FDA enforcement", raw="Subject to an FDA enforcement notice")
    assert await src.check(None, claim) is None


async def test_a_document_naming_the_company_but_from_another_agency_is_no_signal() -> None:
    """Both guards, not either: the API's agency filter can be widened by its
    own agency hierarchy."""
    src = FederalRegisterSource(
        fetch=_fetch(
            _results(
                _document(
                    title="Northwind Diagnostics Inc.; Notice",
                    agencies=[
                        {
                            "name": "Environmental Protection Agency",
                            "slug": "environmental-protection-agency",
                        }
                    ],
                )
            )
        )
    )
    claim = _claim(attribute="FDA enforcement", raw="Subject to an FDA enforcement notice")
    assert await src.check(None, claim) is None


async def test_a_one_word_company_name_is_too_common_to_search_by() -> None:
    """ "Block" appears in thousands of agency notices that have nothing to do
    with the company, and this source has no resolved entity to disambiguate
    against yet."""
    seen: list[str] = []
    src = FederalRegisterSource(fetch=_fetch(_results(_document(title="Block; Notice")), seen=seen))
    claim = _claim(entity="Block", attribute="FDA enforcement", raw="Named in an FDA notice")

    assert await src.check(None, claim) is None
    assert seen == []  # not even searched


async def test_a_single_long_company_name_is_distinctive_enough() -> None:
    src = FederalRegisterSource(
        fetch=_fetch(_results(_document(title="Nanosurgical; Premarket Notice")))
    )
    claim = _claim(entity="Nanosurgical", attribute="FDA clearance", raw="FDA clearance granted")

    verdict = await src.check(None, claim)

    assert verdict is not None and verdict.agrees is True


async def test_an_agency_abbreviation_inside_a_longer_word_is_not_an_agency() -> None:
    """Whole-token matching: "epa" is the agency standing alone and nothing at
    all inside "therapeutic"."""
    seen: list[str] = []
    src = FederalRegisterSource(fetch=_fetch(_results(_document()), seen=seen))
    claim = _claim(attribute="Therapeutic pipeline", raw="Two therapeutics in trials")

    assert await src.check(None, claim) is None
    assert seen == []


async def test_the_agency_filter_is_sent_on_the_entity_lane() -> None:
    seen: list[str] = []
    src = FederalRegisterSource(fetch=_fetch(_results(), seen=seen))
    claim = _claim(attribute="FDA clearance", raw="Pursuing FDA clearance")

    await src.check(None, claim)

    assert len(seen) == 1
    assert "conditions%5Bagencies%5D%5B%5D=food-and-drug-administration" in seen[0]


# --------------------------------------------------------------------------
# It never disagrees, and never breaks.
# --------------------------------------------------------------------------


async def test_this_source_never_returns_a_disagreement() -> None:
    """The contract in one test. Absence from the Federal Register is not
    evidence a regulatory fact is false, and `conflicted` is sticky."""
    fetches = [
        _results(),
        _results(_document(cfr_references=[{"title": 40, "part": 60}])),
        _results(_document(agencies=[])),
    ]
    claims = [
        _claim(raw="Device cleared under 21 CFR 820"),
        _claim(raw="Device cleared under 21 CFR 820"),
        _claim(attribute="FDA clearance", raw="FDA clearance granted"),
    ]
    for payload, claim in zip(fetches, claims, strict=True):
        verdict = await FederalRegisterSource(fetch=_fetch(payload)).check(None, claim)
        assert verdict is None or verdict.agrees is True


async def test_a_fetch_that_raises_is_no_signal() -> None:
    src = FederalRegisterSource(fetch=_fetch(raises=True))
    assert await src.check(None, _claim(raw="Device cleared under 21 CFR 820")) is None


@pytest.mark.parametrize(
    "payload", [None, {}, [], "not json", {"results": "nope"}, {"results": [None, 1, "x"]}]
)
async def test_a_malformed_response_is_no_signal(payload: Any) -> None:
    src = FederalRegisterSource(fetch=_fetch(payload))
    assert await src.check(None, _claim(raw="Device cleared under 21 CFR 820")) is None


def test_the_source_is_not_registered_yet() -> None:
    """Registration attaches once SIM-416 settles the corroboration pass's I/O
    placement -- until then a network call must not sit inside the verify
    transaction."""
    from app.services.corroboration import CORROBORATION_SOURCES

    assert not any(
        getattr(s, "name", None) == FederalRegisterSource.name for s in CORROBORATION_SOURCES
    )
