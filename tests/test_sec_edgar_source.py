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


def _facts(concept: str, year: int, val: float, *, form: str = "10-K") -> dict:
    return {
        "facts": {
            "us-gaap": {
                concept: {
                    "units": {
                        "USD": [
                            {
                                "fy": year,
                                "fp": "FY",
                                "form": form,
                                "filed": "2024-02-01",
                                "val": val,
                            }
                        ]
                    }
                }
            }
        }
    }


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
    entity="Apple Inc.", attribute="revenue", period_year=2023, normalized=1000.0, unit="USD"
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


async def test_no_signal_when_a_fetch_raises():
    fetch = _fake_fetch(
        _facts("Revenues", 2023, 1000.0),
        fail_urls=("https://www.sec.gov/files/company_tickers.json",),
    )
    assert await SecEdgarSource(fetch=fetch).check(None, _claim()) is None


def test_lookup_prefers_10k_and_latest_filed_on_restatement():
    facts = {
        "facts": {
            "us-gaap": {
                "Assets": {
                    "units": {
                        "USD": [
                            {
                                "fy": 2023,
                                "fp": "FY",
                                "form": "10-Q",
                                "filed": "2024-01-01",
                                "val": 5.0,
                            },
                            {
                                "fy": 2023,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2024-02-01",
                                "val": 9.0,
                            },
                            {
                                "fy": 2023,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2024-03-01",
                                "val": 10.0,
                            },
                        ]
                    }
                }
            }
        }
    }
    assert _lookup_annual_fact(facts, ("Assets",), 2023) == ("Assets", 10.0)
