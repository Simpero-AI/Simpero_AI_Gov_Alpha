"""Hermetic unit tests for the SEC EDGAR corroboration source (Epic 12). No
network and no DB: `fetch` is injected, and check() never touches the session."""

import pytest

from app.models.claim import Claim
from app.services.corroboration import CorroborationVerdict
from app.services.corroboration_sources.sec_edgar import SecEdgarSource, _lookup_annual_fact

_TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corporation"},
    # Same title under two CIKs -> ambiguous, must resolve to nothing.
    "2": {"cik_str": 111, "ticker": "DUPA", "title": "Dupe Co"},
    "3": {"cik_str": 222, "ticker": "DUPB", "title": "Dupe Co"},
}


def _annual(year: int, val: float, *, form: str = "10-K", filed: str = "2024-02-01") -> dict:
    """One full-fiscal-year (duration) EDGAR USD datapoint, shaped like real
    companyfacts: the period lives in `start`/`end`, and `fy` is the FILING year.
    Real EDGAR tags a 10-K's prior-year comparatives with that same `fy`/`fp`, so
    keying on `fy` would pull them in -- these fixtures make that visible."""
    return {
        "fy": year,
        "fp": "FY",
        "form": form,
        "filed": filed,
        "start": f"{year}-01-01",
        "end": f"{year}-12-31",
        "frame": f"CY{year}",
        "val": val,
    }


def _facts(concept: str, year: int, val: float, *, form: str = "10-K") -> dict:
    return {"facts": {"us-gaap": {concept: {"units": {"USD": [_annual(year, val, form=form)]}}}}}


def _fake_fetch(facts: dict | None = None, *, fail_urls: tuple[str, ...] = ()):
    async def fetch(url: str):
        if url in fail_urls:
            raise RuntimeError("boom")
        if "company_tickers" in url:
            return _TICKERS
        if "companyfacts" in url:
            return facts or {}
        raise AssertionError(f"unexpected url {url}")

    return fetch


def _claim(
    entity="Apple Inc.",
    attribute="revenue",
    period_year=2023,
    normalized=1000.0,
    unit: str | None = "USD",
) -> Claim:
    return Claim(
        entity=entity,
        attribute=attribute,
        period_year=period_year,
        value={"normalized": normalized, "unit": unit},
    )


async def test_agrees_when_edgar_matches_within_tolerance():
    src = SecEdgarSource(fetch=_fake_fetch(_facts("Revenues", 2023, 1000.0)))
    v = await src.check(None, _claim(normalized=1000.0))
    assert isinstance(v, CorroborationVerdict)
    assert v.agrees is True
    assert v.result["cik"] == 320193
    assert v.result["concept"] == "Revenues"
    assert v.result["edgar_value"] == 1000.0


async def test_disagrees_on_material_delta_and_records_both_values():
    src = SecEdgarSource(fetch=_fake_fetch(_facts("Revenues", 2023, 2000.0)))
    v = await src.check(None, _claim(normalized=1000.0))
    assert v is not None and v.agrees is False
    assert v.result["claim_value"] == 1000.0
    assert v.result["edgar_value"] == 2000.0
    assert v.result["discrepancy_delta"] == pytest.approx(0.5)


async def test_no_signal_when_company_is_not_an_edgar_filer():
    src = SecEdgarSource(fetch=_fake_fetch(_facts("Revenues", 2023, 1000.0)))
    assert await src.check(None, _claim(entity="Some Private Startup LLC")) is None


async def test_no_signal_on_ambiguous_company_name():
    src = SecEdgarSource(fetch=_fake_fetch(_facts("Revenues", 2023, 1000.0)))
    assert await src.check(None, _claim(entity="Dupe Co")) is None


async def test_no_signal_for_an_unmapped_attribute():
    src = SecEdgarSource(fetch=_fake_fetch(_facts("Revenues", 2023, 1000.0)))
    assert await src.check(None, _claim(attribute="headcount")) is None


async def test_no_signal_when_no_fact_for_the_period():
    src = SecEdgarSource(fetch=_fake_fetch(_facts("Revenues", 2019, 1000.0)))
    assert await src.check(None, _claim(period_year=2023)) is None


async def test_no_signal_for_a_non_usd_unit():
    src = SecEdgarSource(fetch=_fake_fetch(_facts("Revenues", 2023, 1000.0)))
    assert await src.check(None, _claim(unit="CAD")) is None


async def test_no_signal_for_a_missing_unit():
    """A missing unit is unknown currency, not implicit USD -- a CAD figure with
    no unit compared against EDGAR USD would be a false conflict."""
    src = SecEdgarSource(fetch=_fake_fetch(_facts("Revenues", 2023, 1000.0)))
    assert await src.check(None, _claim(unit=None)) is None


async def test_no_signal_when_the_claim_scale_was_assumed():
    """assumed_1x means the magnitude was never detected, so `normalized` may be
    off by 10^3/10^6; comparing it against an absolute EDGAR figure fabricates a
    delta."""
    src = SecEdgarSource(fetch=_fake_fetch(_facts("Revenues", 2023, 1000.0)))
    claim = Claim(
        entity="Apple Inc.",
        attribute="revenue",
        period_year=2023,
        value={"normalized": 1000.0, "unit": "USD", "scale_source": "assumed_1x"},
    )
    assert await src.check(None, claim) is None


async def test_no_false_conflict_against_a_prior_year_comparative():
    """End to end: a correct 2023 claim against a 10-K that also carries the 2021
    comparative (same fy tag) must agree on the 2023 value, never conflict on the
    2021 one -- the false-conflict this adapter's period selection must prevent."""
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [_annual(2021, 700.0), _annual(2023, 1000.0)]}}
            }
        }
    }
    for u in facts["facts"]["us-gaap"]["Revenues"]["units"]["USD"]:
        u["fy"] = 2023
    src = SecEdgarSource(fetch=_fake_fetch(facts))
    v = await src.check(None, _claim(period_year=2023, normalized=1000.0))
    assert v is not None and v.agrees is True
    assert v.result["edgar_value"] == 1000.0


async def test_no_signal_when_a_fetch_raises():
    fetch = _fake_fetch(
        _facts("Revenues", 2023, 1000.0),
        fail_urls=("https://www.sec.gov/files/company_tickers.json",),
    )
    assert await SecEdgarSource(fetch=fetch).check(None, _claim()) is None


def test_lookup_prefers_10k_and_latest_filed_on_restatement():
    """Same period (end 2023-12-31, an instant balance-sheet value with no
    `start`) re-reported across filings: 10-K beats 10-Q, and the latest-filed
    restatement -- here the 2023 value as re-reported in the fy=2024 10-K --
    supersedes the originally-filed one."""
    facts = {
        "facts": {
            "us-gaap": {
                "Assets": {
                    "units": {
                        "USD": [
                            {
                                "fy": 2023,
                                "form": "10-Q",
                                "filed": "2024-01-01",
                                "end": "2023-12-31",
                                "val": 5.0,
                            },
                            {
                                "fy": 2023,
                                "form": "10-K",
                                "filed": "2024-02-01",
                                "end": "2023-12-31",
                                "val": 9.0,
                            },
                            {
                                "fy": 2024,
                                "form": "10-K",
                                "filed": "2025-02-01",
                                "end": "2023-12-31",
                                "val": 10.0,
                            },
                        ]
                    }
                }
            }
        }
    }
    assert _lookup_annual_fact(facts, ("Assets",), 2023) == ("Assets", 10.0)


def test_lookup_prefers_a_later_10ka_amendment_over_the_original_10k():
    """A 10-K/A is how a company restates a wrong annual figure; filed after the
    original 10-K, it must supersede it -- not be ignored for failing an exact
    "10-K" match, which would silently keep the stale, un-restated number."""
    facts = {
        "facts": {
            "us-gaap": {
                "Assets": {
                    "units": {
                        "USD": [
                            {
                                "fy": 2023,
                                "form": "10-K",
                                "filed": "2024-02-01",
                                "end": "2023-12-31",
                                "val": 9.0,
                            },
                            {
                                "fy": 2023,
                                "form": "10-K/A",
                                "filed": "2024-06-01",
                                "end": "2023-12-31",
                                "val": 12.0,
                            },
                        ]
                    }
                }
            }
        }
    }
    assert _lookup_annual_fact(facts, ("Assets",), 2023) == ("Assets", 12.0)


def test_lookup_selects_the_claimed_period_not_a_prior_year_comparative():
    """The critical case: one 10-K (fy=2023) carries the 2023 primary AND the
    2022/2021 comparatives, ALL tagged fy:2023, fp:"FY", same filed date. Keying
    on fy would pull the prior years in and let a correct 2023 claim be compared
    against the 2021 number. Selecting on the value's own start/end picks the
    right year for each."""
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            _annual(2021, 700.0),
                            _annual(2022, 850.0),
                            _annual(2023, 1000.0),
                        ]
                    }
                }
            }
        }
    }
    # All three share fy:2023 in real EDGAR; force that to prove fy is ignored.
    for u in facts["facts"]["us-gaap"]["Revenues"]["units"]["USD"]:
        u["fy"] = 2023
    assert _lookup_annual_fact(facts, ("Revenues",), 2023) == ("Revenues", 1000.0)
    assert _lookup_annual_fact(facts, ("Revenues",), 2022) == ("Revenues", 850.0)


def test_a_quarterly_datapoint_does_not_stand_in_for_the_annual_figure():
    """A duration value ending in 2023 but spanning only a quarter is not the
    annual figure -- it must not be selected."""
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "fy": 2023,
                                "form": "10-Q",
                                "filed": "2023-11-01",
                                "start": "2023-10-01",
                                "end": "2023-12-31",
                                "val": 250.0,
                            },
                        ]
                    }
                }
            }
        }
    }
    assert _lookup_annual_fact(facts, ("Revenues",), 2023) is None


async def test_companyfacts_is_fetched_once_across_claims_for_a_company():
    # run_corroboration calls check() once PER claim; the same company's (large)
    # companyfacts file must be fetched once, not once per claim -- SEC fair-access.
    calls = {"tickers": 0, "companyfacts": 0}

    async def counting_fetch(url: str):
        if "company_tickers" in url:
            calls["tickers"] += 1
            return _TICKERS
        if "companyfacts" in url:
            calls["companyfacts"] += 1
            return _facts("Revenues", 2023, 1000.0)
        raise AssertionError(f"unexpected url {url}")

    src = SecEdgarSource(fetch=counting_fetch)
    for year in (2021, 2022, 2023):
        await src.check(None, _claim(period_year=year, normalized=1000.0))

    assert calls["tickers"] == 1
    assert calls["companyfacts"] == 1  # cached per CIK, not re-fetched per claim


async def test_a_failed_companyfacts_fetch_is_attempted_once_not_per_claim():
    # A bad/unreachable CIK must not be re-fetched for every claim -- the failure
    # is cached (best-effort: no-signal), bounding SEC requests.
    calls = {"companyfacts": 0}

    async def failing_facts(url: str):
        if "company_tickers" in url:
            return _TICKERS
        if "companyfacts" in url:
            calls["companyfacts"] += 1
            raise RuntimeError("boom")
        raise AssertionError(f"unexpected url {url}")

    src = SecEdgarSource(fetch=failing_facts)
    for year in (2021, 2022, 2023):
        assert await src.check(None, _claim(period_year=year)) is None

    assert calls["companyfacts"] == 1


def test_sec_edgar_is_registered():
    # The one source turned on today (keyless, no resolved-entity dependency,
    # validated shapes). The other adapters keep their own not-registered guards.
    from app.services.corroboration import CORROBORATION_SOURCES

    assert any(getattr(s, "name", None) == SecEdgarSource.name for s in CORROBORATION_SOURCES)
