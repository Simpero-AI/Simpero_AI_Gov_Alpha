"""Hermetic unit tests for the SEC EDGAR corroboration source (Epic 12). No
network and no DB: `fetch` (companyfacts) and `resolve` (the deal-scoped resolved
entity carrying the SEC CIK) are injected; check() keys on the resolved CIK, never
on claim.entity."""

import uuid

import pytest

from app.models.claim import Claim
from app.models.resolved_entity import REGISTRY_CIK
from app.services.corroboration import CorroborationVerdict
from app.services.corroboration_sources.sec_edgar import SecEdgarSource, _lookup_annual_fact
from app.services.entity_resolution.resolved import DealEntity


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


def _fake_fetch(facts: dict | None = None, *, fail: bool = False):
    async def fetch(url: str):
        if fail:
            raise RuntimeError("boom")
        if "companyfacts" in url:
            return facts or {}
        raise AssertionError(f"unexpected url {url}")

    return fetch


def _fake_resolve(cik: str | None = "320193"):
    """A resolve() stub: a DealEntity carrying `cik` in its SEC registry_id, or
    None when cik is None (no registry resolved this deal)."""

    async def resolve(db, deal_id):
        if cik is None:
            return None
        return DealEntity(
            deal_id=deal_id, canonical_name="Test Co", registry_ids={REGISTRY_CIK: cik}
        )

    return resolve


def _resolve_without_cik():
    async def resolve(db, deal_id):
        return DealEntity(deal_id=deal_id, canonical_name="Test Co", registry_ids={})

    return resolve


def _claim(
    entity="Apple Inc.",
    attribute="revenue",
    period_year=2023,
    normalized=1000.0,
    unit: str | None = "USD",
    deal_id: uuid.UUID | None = None,
) -> Claim:
    return Claim(
        entity=entity,
        attribute=attribute,
        period_year=period_year,
        deal_id=deal_id or uuid.uuid4(),
        value={"normalized": normalized, "unit": unit},
    )


async def test_agrees_when_edgar_matches_within_tolerance():
    src = SecEdgarSource(
        fetch=_fake_fetch(_facts("Revenues", 2023, 1000.0)), resolve=_fake_resolve()
    )
    v = await src.check(None, _claim(normalized=1000.0))
    assert isinstance(v, CorroborationVerdict)
    assert v.agrees is True
    assert v.result["cik"] == 320193
    assert v.result["concept"] == "Revenues"
    assert v.result["edgar_value"] == 1000.0


async def test_disagrees_on_material_delta_and_records_both_values():
    src = SecEdgarSource(
        fetch=_fake_fetch(_facts("Revenues", 2023, 2000.0)), resolve=_fake_resolve()
    )
    v = await src.check(None, _claim(normalized=1000.0))
    assert v is not None and v.agrees is False
    assert v.result["claim_value"] == 1000.0
    assert v.result["edgar_value"] == 2000.0
    assert v.result["discrepancy_delta"] == pytest.approx(0.5)


async def test_no_signal_when_no_resolved_entity():
    # No registry resolved this deal -> nothing to compare against, never a
    # name-matched guess.
    src = SecEdgarSource(
        fetch=_fake_fetch(_facts("Revenues", 2023, 1000.0)), resolve=_fake_resolve(cik=None)
    )
    assert await src.check(None, _claim()) is None


async def test_no_signal_when_the_deal_has_no_sec_cik():
    # A resolved entity that no SEC lookup answered (no CIK) is no-signal -- the
    # adapter must not fall back to matching claim.entity by name.
    src = SecEdgarSource(
        fetch=_fake_fetch(_facts("Revenues", 2023, 1000.0)), resolve=_resolve_without_cik()
    )
    assert await src.check(None, _claim()) is None


async def test_no_signal_for_an_unmapped_attribute():
    src = SecEdgarSource(
        fetch=_fake_fetch(_facts("Revenues", 2023, 1000.0)), resolve=_fake_resolve()
    )
    assert await src.check(None, _claim(attribute="headcount")) is None


async def test_no_signal_when_no_fact_for_the_period():
    src = SecEdgarSource(
        fetch=_fake_fetch(_facts("Revenues", 2019, 1000.0)), resolve=_fake_resolve()
    )
    assert await src.check(None, _claim(period_year=2023)) is None


async def test_no_signal_for_a_non_usd_unit():
    src = SecEdgarSource(
        fetch=_fake_fetch(_facts("Revenues", 2023, 1000.0)), resolve=_fake_resolve()
    )
    assert await src.check(None, _claim(unit="CAD")) is None


async def test_no_signal_for_a_missing_unit():
    """A missing unit is unknown currency, not implicit USD -- a CAD figure with
    no unit compared against EDGAR USD would be a false conflict."""
    src = SecEdgarSource(
        fetch=_fake_fetch(_facts("Revenues", 2023, 1000.0)), resolve=_fake_resolve()
    )
    assert await src.check(None, _claim(unit=None)) is None


async def test_no_signal_when_the_claim_scale_was_assumed():
    """assumed_1x means the magnitude was never detected, so `normalized` may be
    off by 10^3/10^6; comparing it against an absolute EDGAR figure fabricates a
    delta."""
    src = SecEdgarSource(
        fetch=_fake_fetch(_facts("Revenues", 2023, 1000.0)), resolve=_fake_resolve()
    )
    claim = Claim(
        entity="Apple Inc.",
        attribute="revenue",
        period_year=2023,
        deal_id=uuid.uuid4(),
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
    src = SecEdgarSource(fetch=_fake_fetch(facts), resolve=_fake_resolve())
    v = await src.check(None, _claim(period_year=2023, normalized=1000.0))
    assert v is not None and v.agrees is True
    assert v.result["edgar_value"] == 1000.0


async def test_no_signal_when_the_facts_fetch_raises():
    src = SecEdgarSource(fetch=_fake_fetch(fail=True), resolve=_fake_resolve())
    assert await src.check(None, _claim()) is None


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
    # run_corroboration calls check() once PER claim; the same CIK's (large)
    # companyfacts file must be fetched once, not once per claim -- SEC fair-access.
    calls = {"companyfacts": 0}

    async def counting_fetch(url: str):
        if "companyfacts" in url:
            calls["companyfacts"] += 1
            return _facts("Revenues", 2023, 1000.0)
        raise AssertionError(f"unexpected url {url}")

    src = SecEdgarSource(fetch=counting_fetch, resolve=_fake_resolve())
    for year in (2021, 2022, 2023):
        await src.check(None, _claim(period_year=year, normalized=1000.0))

    assert calls["companyfacts"] == 1  # cached per CIK, not re-fetched per claim


async def test_a_failed_companyfacts_fetch_is_attempted_once_not_per_claim():
    # A bad/unreachable CIK must not be re-fetched for every claim -- the failure
    # is cached (best-effort: no-signal), bounding SEC requests.
    calls = {"companyfacts": 0}

    async def failing_facts(url: str):
        if "companyfacts" in url:
            calls["companyfacts"] += 1
            raise RuntimeError("boom")
        raise AssertionError(f"unexpected url {url}")

    src = SecEdgarSource(fetch=failing_facts, resolve=_fake_resolve())
    for year in (2021, 2022, 2023):
        assert await src.check(None, _claim(period_year=year)) is None

    assert calls["companyfacts"] == 1


def test_sec_edgar_is_registered():
    # Registered, but it keys on the resolved CIK -- a deal with no SEC-resolved
    # entity is a clean no-signal, never a name-matched guess.
    from app.services.corroboration import CORROBORATION_SOURCES

    assert any(getattr(s, "name", None) == SecEdgarSource.name for s in CORROBORATION_SOURCES)


async def test_cache_expires_after_the_ttl(monkeypatch):
    # With the TTL elapsed, a refiled company is picked up rather than served stale
    # forever -- each check re-fetches once the entry is stale.
    import app.services.corroboration_sources.sec_edgar as edgar_mod

    monkeypatch.setattr(edgar_mod, "_CACHE_TTL_S", -1.0)  # every entry immediately stale
    calls = {"companyfacts": 0}

    async def counting_fetch(url: str):
        if "companyfacts" in url:
            calls["companyfacts"] += 1
            return _facts("Revenues", 2023, 1000.0)
        raise AssertionError(f"unexpected url {url}")

    src = SecEdgarSource(fetch=counting_fetch, resolve=_fake_resolve())
    for year in (2021, 2022, 2023):
        await src.check(None, _claim(period_year=year, normalized=1000.0))

    assert calls["companyfacts"] == 3


async def test_the_facts_cache_is_bounded_and_evicts_the_oldest(monkeypatch):
    # Past the size bound the oldest CIK entry is evicted, so a long-running worker
    # cannot grow the cache without limit.
    import app.services.corroboration_sources.sec_edgar as edgar_mod

    monkeypatch.setattr(edgar_mod, "_MAX_CACHED_FACTS", 1)
    apple, msft = uuid.uuid4(), uuid.uuid4()
    ciks = {apple: "320193", msft: "789019"}

    async def resolve(db, deal_id):
        return DealEntity(
            deal_id=deal_id, canonical_name="X", registry_ids={REGISTRY_CIK: ciks[deal_id]}
        )

    calls = {"companyfacts": 0}

    async def counting_fetch(url: str):
        if "companyfacts" in url:
            calls["companyfacts"] += 1
            return _facts("Revenues", 2023, 1000.0)
        raise AssertionError(f"unexpected url {url}")

    src = SecEdgarSource(fetch=counting_fetch, resolve=resolve)
    await src.check(None, _claim(deal_id=apple, period_year=2023))
    await src.check(None, _claim(deal_id=msft, period_year=2023))
    assert len(src._facts) == 1  # the first CIK is evicted when the second is cached

    # The evicted CIK must be re-fetched -- proof the bound is enforced.
    await src.check(None, _claim(deal_id=apple, period_year=2023))
    assert calls["companyfacts"] == 3
