"""Screening #2: one Y/N case and one `unknown` case per deterministic
evaluator (the ticket's explicit acceptance requirement), plus the extra
sub-cases db_02's zero-burn guard and gs_04/db_07's shared-claim,
independent-thresholds behavior call for.

Uses the same db_session/org_a_id fixtures (tests/conftest.py) and bare-Claim
-construction pattern tests/test_consistency.py already established.
"""

from __future__ import annotations

import uuid

from app.models import Claim
from app.repo.DealRepo import DealRepo
from app.repo.InvestmentProfileRepo import InvestmentProfileRepo
from app.services.screening.evaluators.deterministic import EVALUATORS
from app.services.screening.rulebook import load_rulebook
from app.services.screening.types import ClaimRef, RuleResult

RULEBOOK = load_rulebook()


def _reason(result: RuleResult) -> str:
    assert result.reason is not None
    return result.reason


async def _seed_deal(db_session, org_a_id, **fields):
    deal = await DealRepo(db_session).create({"org_id": org_a_id, "name": "Test Deal", **fields})
    await db_session.flush()
    return deal


def _claim(
    *,
    org_id: int,
    deal_id: uuid.UUID,
    attribute: str,
    normalized: float,
    period_year: int | None = 2024,
    status: str = "verified",
    verification_method: str | None = "human_review",
    flags: list[str] | None = None,
    unit: str | None = "USD",
    value_type: str = "currency",
) -> Claim:
    """`unit`/`value_type` default to the currency shape most rules read, but
    are overridable: customer_concentration arrives from the parser as a
    PERCENT at face value (normalized=62.0, unit="%"), not a fraction, and
    seeding it as currency-0.62 is what hid the units bug these tests now
    cover -- see test_gs_04_reads_a_percent_claim_as_a_fraction."""
    return Claim(
        org_id=org_id,
        deal_id=deal_id,
        entity="TestCo",
        attribute=attribute,
        period_year=period_year,
        value={
            "raw": str(normalized),
            "normalized": normalized,
            "unit": unit,
            "scale_multiplier": 1,
            "scale_source": "explicit_in_value",
            "value_type": value_type,
        },
        kind="pdf",
        page=1,
        char_start=0,
        char_end=1,
        status=status,
        verification_method=verification_method,
        flags=flags,
    )


async def _evaluate(rule_id: str, session, deal):
    return await EVALUATORS[rule_id](session, deal, RULEBOOK)


# --- gs_03: paying customer -------------------------------------------------


async def test_gs_03_y_when_revenue_positive(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id)
    db_session.add(
        _claim(org_id=org_a_id, deal_id=deal.id, attribute="revenue", normalized=100_000)
    )
    await db_session.flush()

    result = await _evaluate("gs_03", db_session, deal)
    assert result.verdict == "Y"
    assert result.evidence is not None


async def test_gs_03_unknown_when_no_revenue_claim(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id)

    result = await _evaluate("gs_03", db_session, deal)
    assert result.verdict == "unknown"
    assert result.evidence is None
    assert "no revenue claim" in _reason(result)


async def test_gs_03_untrusted_claim_status_treated_as_absent(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id)
    db_session.add(
        _claim(
            org_id=org_a_id,
            deal_id=deal.id,
            attribute="revenue",
            normalized=100_000,
            status="proposed",
            verification_method=None,
        )
    )
    await db_session.flush()

    result = await _evaluate("gs_03", db_session, deal)
    assert result.verdict == "unknown"


# --- gs_04 / db_07: shared customer_concentration, independent thresholds --


def _concentration(*, org_id: int, deal_id: uuid.UUID, percent: float) -> Claim:
    """A customer_concentration claim in the shape the parser really emits:
    a percent read at FACE VALUE (75% -> normalized 75.0, unit "%"), per
    parser_service/scale.py::_self_scaling."""
    return _claim(
        org_id=org_id,
        deal_id=deal_id,
        attribute="customer_concentration",
        normalized=percent,
        unit="%",
        value_type="percent",
    )


async def test_gs_04_and_db_07_share_one_claim_different_verdicts(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id)
    # 75% trips db_07's >0.70 breaker and fails gs_04's <=0.50 green signal.
    db_session.add(_concentration(org_id=org_a_id, deal_id=deal.id, percent=75.0))
    await db_session.flush()

    gs_04 = await _evaluate("gs_04", db_session, deal)
    db_07 = await _evaluate("db_07", db_session, deal)
    assert gs_04.verdict == "N"
    assert db_07.verdict == "Y"
    assert isinstance(gs_04.evidence, ClaimRef)
    assert isinstance(db_07.evidence, ClaimRef)
    assert gs_04.evidence.claim_id == db_07.evidence.claim_id


async def test_gs_04_y_when_concentration_low(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id)
    db_session.add(_concentration(org_id=org_a_id, deal_id=deal.id, percent=30.0))
    await db_session.flush()

    result = await _evaluate("gs_04", db_session, deal)
    assert result.verdict == "Y"


async def test_gs_04_reads_a_percent_claim_as_a_fraction(db_session, org_a_id):
    """Regression: the parser emits a percent at face value, the rulebook's
    thresholds are fractions. Comparing 30.0 against 0.50 directly made a
    HEALTHY 30% concentration fail gs_04's must-have."""
    deal = await _seed_deal(db_session, org_a_id)
    db_session.add(_concentration(org_id=org_a_id, deal_id=deal.id, percent=30.0))
    await db_session.flush()

    assert (await _evaluate("gs_04", db_session, deal)).verdict == "Y"


async def test_db_07_does_not_auto_decline_a_healthy_concentration(db_session, org_a_id):
    """Regression, the severe half: 30.0 > 0.70 was True, so a company whose
    largest customer is 30% of revenue was AUTO-DECLINED by the deal-breaker.
    A 30% concentration is nowhere near db_07's 70% bar."""
    deal = await _seed_deal(db_session, org_a_id)
    db_session.add(_concentration(org_id=org_a_id, deal_id=deal.id, percent=30.0))
    await db_session.flush()

    assert (await _evaluate("db_07", db_session, deal)).verdict == "N"


async def test_concentration_boundary_is_exact_at_the_threshold(db_session, org_a_id):
    """50.0% is `<= 0.50` (gs_04 Y) and 70.0% is not `> 0.70` (db_07 N) --
    the two thresholds are inclusive/exclusive respectively, and float
    division by 100 must not push either off its own boundary."""
    deal_50 = await _seed_deal(db_session, org_a_id)
    db_session.add(_concentration(org_id=org_a_id, deal_id=deal_50.id, percent=50.0))
    deal_70 = await _seed_deal(db_session, org_a_id)
    db_session.add(_concentration(org_id=org_a_id, deal_id=deal_70.id, percent=70.0))
    await db_session.flush()

    assert (await _evaluate("gs_04", db_session, deal_50)).verdict == "Y"
    assert (await _evaluate("db_07", db_session, deal_70)).verdict == "N"


async def test_concentration_as_a_ratio_claim_is_read_directly(db_session, org_a_id):
    """The other legitimate shape: unit "ratio", already 0-1, no division."""
    deal = await _seed_deal(db_session, org_a_id)
    db_session.add(
        _claim(
            org_id=org_a_id,
            deal_id=deal.id,
            attribute="customer_concentration",
            normalized=0.75,
            unit="ratio",
            value_type="ratio",
        )
    )
    await db_session.flush()

    assert (await _evaluate("gs_04", db_session, deal)).verdict == "N"
    assert (await _evaluate("db_07", db_session, deal)).verdict == "Y"


async def test_concentration_in_unreadable_units_is_unknown_not_a_verdict(db_session, org_a_id):
    """A concentration claim carrying a currency unit is a figure we cannot
    interpret as a share. It must reach a human -- crucially db_07 must NOT
    fall through to N, which would silently clear an auto-decline."""
    deal = await _seed_deal(db_session, org_a_id)
    db_session.add(
        _claim(
            org_id=org_a_id,
            deal_id=deal.id,
            attribute="customer_concentration",
            normalized=0.75,
            unit="USD",
            value_type="currency",
        )
    )
    await db_session.flush()

    gs_04 = await _evaluate("gs_04", db_session, deal)
    db_07 = await _evaluate("db_07", db_session, deal)
    assert gs_04.verdict == "unknown"
    assert db_07.verdict == "unknown"
    assert "0-1 share" in _reason(db_07)
    # Evidence still points at the offending claim so a human can go look.
    assert isinstance(db_07.evidence, ClaimRef)


async def test_concentration_out_of_range_is_unknown(db_session, org_a_id):
    """620% is a mis-scaled figure, not a real concentration. Fail closed."""
    deal = await _seed_deal(db_session, org_a_id)
    db_session.add(_concentration(org_id=org_a_id, deal_id=deal.id, percent=620.0))
    await db_session.flush()

    assert (await _evaluate("gs_04", db_session, deal)).verdict == "unknown"
    assert (await _evaluate("db_07", db_session, deal)).verdict == "unknown"


async def test_gs_04_unknown_when_not_extracted(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id)
    result = await _evaluate("gs_04", db_session, deal)
    assert result.verdict == "unknown"


async def test_db_07_unknown_when_not_extracted(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id)
    result = await _evaluate("db_07", db_session, deal)
    assert result.verdict == "unknown"


# --- gs_07: HQ geography ----------------------------------------------------


async def test_gs_07_y_when_in_approved_list(db_session, org_a_id):
    await InvestmentProfileRepo(db_session).create(
        {"org_id": org_a_id, "mandate": {"approved_geographies": ["US", "CA"]}}
    )
    deal = await _seed_deal(db_session, org_a_id, hq_geography="US")
    await db_session.flush()

    result = await _evaluate("gs_07", db_session, deal)
    assert result.verdict == "Y"


async def test_gs_07_n_when_not_in_approved_list(db_session, org_a_id):
    await InvestmentProfileRepo(db_session).create(
        {"org_id": org_a_id, "mandate": {"approved_geographies": ["US", "CA"]}}
    )
    deal = await _seed_deal(db_session, org_a_id, hq_geography="FR")
    await db_session.flush()

    result = await _evaluate("gs_07", db_session, deal)
    assert result.verdict == "N"


async def test_gs_07_unknown_when_hq_geography_unset(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id)
    result = await _evaluate("gs_07", db_session, deal)
    assert result.verdict == "unknown"


async def test_gs_07_unknown_when_workspace_config_missing(db_session, org_a_id):
    """A rule can't fire against a policy that was never set -- must not be
    silently treated as 'nothing approved' (a false N)."""
    deal = await _seed_deal(db_session, org_a_id, hq_geography="US")
    result = await _evaluate("gs_07", db_session, deal)
    assert result.verdict == "unknown"


# --- gs_08: approved sector -------------------------------------------------


async def test_gs_08_y_when_in_approved_list(db_session, org_a_id):
    await InvestmentProfileRepo(db_session).create(
        {"org_id": org_a_id, "mandate": {"approved_sectors": ["saas", "fintech"]}}
    )
    deal = await _seed_deal(db_session, org_a_id, sector="saas")
    await db_session.flush()

    result = await _evaluate("gs_08", db_session, deal)
    assert result.verdict == "Y"


async def test_gs_08_unknown_when_sector_unset(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id)
    result = await _evaluate("gs_08", db_session, deal)
    assert result.verdict == "unknown"


# --- db_04: prohibited sector (fixed list, straight from the rulebook) -----


async def test_db_04_y_when_sector_prohibited(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id, sector="cannabis")
    result = await _evaluate("db_04", db_session, deal)
    assert result.verdict == "Y"


async def test_db_04_n_when_sector_not_prohibited(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id, sector="saas")
    result = await _evaluate("db_04", db_session, deal)
    assert result.verdict == "N"


async def test_db_04_unknown_when_sector_unset(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id)
    result = await _evaluate("db_04", db_session, deal)
    assert result.verdict == "unknown"


async def test_db_04_reads_prohibited_list_from_rulebook_not_hardcoded(db_session, org_a_id):
    """The rulebook is the single source of truth for this list -- confirm a
    sector from track_b.yaml's own threshold.in actually fires, so the
    evaluator can't silently be reading a separate Python constant instead."""
    db_04_threshold = RULEBOOK.by_id["db_04"].threshold
    assert db_04_threshold is not None
    prohibited = db_04_threshold["in"]
    assert prohibited  # sanity: the rulebook really carries the list
    deal = await _seed_deal(db_session, org_a_id, sector=prohibited[0])
    result = await _evaluate("db_04", db_session, deal)
    assert result.verdict == "Y"


# --- db_01: gate only --------------------------------------------------------


async def test_db_01_n_when_revenue_positive(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id)
    db_session.add(_claim(org_id=org_a_id, deal_id=deal.id, attribute="revenue", normalized=1000))
    await db_session.flush()

    result = await _evaluate("db_01", db_session, deal)
    assert result.verdict == "N"


async def test_db_01_unknown_deferred_when_revenue_zero(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id)
    db_session.add(_claim(org_id=org_a_id, deal_id=deal.id, attribute="revenue", normalized=0))
    await db_session.flush()

    result = await _evaluate("db_01", db_session, deal)
    assert result.verdict == "unknown"
    assert "not assessable from the CIM" in _reason(result)


async def test_db_01_unknown_when_no_revenue_claim(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id)
    result = await _evaluate("db_01", db_session, deal)
    assert result.verdict == "unknown"
    assert "no revenue claim" in _reason(result)


# --- db_02: gate only --------------------------------------------------------


async def test_db_02_n_when_runway_at_least_six_months(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id)
    db_session.add(
        _claim(
            org_id=org_a_id, deal_id=deal.id, attribute="cash_and_equivalents", normalized=600_000
        )
    )
    db_session.add(
        _claim(org_id=org_a_id, deal_id=deal.id, attribute="monthly_burn", normalized=100_000)
    )
    await db_session.flush()

    result = await _evaluate("db_02", db_session, deal)
    assert result.verdict == "N"


async def test_db_02_unknown_deferred_when_runway_short(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id)
    db_session.add(
        _claim(
            org_id=org_a_id, deal_id=deal.id, attribute="cash_and_equivalents", normalized=200_000
        )
    )
    db_session.add(
        _claim(org_id=org_a_id, deal_id=deal.id, attribute="monthly_burn", normalized=100_000)
    )
    await db_session.flush()

    result = await _evaluate("db_02", db_session, deal)
    assert result.verdict == "unknown"
    assert "not assessable from the CIM" in _reason(result)


async def test_db_02_unknown_when_cash_missing(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id)
    db_session.add(
        _claim(org_id=org_a_id, deal_id=deal.id, attribute="monthly_burn", normalized=100_000)
    )
    await db_session.flush()

    result = await _evaluate("db_02", db_session, deal)
    assert result.verdict == "unknown"
    assert "cash_and_equivalents" in _reason(result)


async def test_db_02_unknown_when_burn_missing(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id)
    db_session.add(
        _claim(
            org_id=org_a_id, deal_id=deal.id, attribute="cash_and_equivalents", normalized=600_000
        )
    )
    await db_session.flush()

    result = await _evaluate("db_02", db_session, deal)
    assert result.verdict == "unknown"
    assert "monthly_burn" in _reason(result)


async def test_db_02_unknown_when_burn_is_zero(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id)
    db_session.add(
        _claim(
            org_id=org_a_id, deal_id=deal.id, attribute="cash_and_equivalents", normalized=600_000
        )
    )
    db_session.add(_claim(org_id=org_a_id, deal_id=deal.id, attribute="monthly_burn", normalized=0))
    await db_session.flush()

    result = await _evaluate("db_02", db_session, deal)
    assert result.verdict == "unknown"
    assert "zero" in _reason(result)
