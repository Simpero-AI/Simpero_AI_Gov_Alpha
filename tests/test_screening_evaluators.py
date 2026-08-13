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
from app.services.screening.evaluators import deterministic as det_module
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
) -> Claim:
    return Claim(
        org_id=org_id,
        deal_id=deal_id,
        entity="TestCo",
        attribute=attribute,
        period_year=period_year,
        value={
            "raw": str(normalized),
            "normalized": normalized,
            "unit": "USD",
            "scale_multiplier": 1,
            "scale_source": "explicit_in_value",
            "value_type": "currency",
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


async def test_gs_04_and_db_07_share_one_claim_different_verdicts(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id)
    # 0.75 trips db_07's >0.70 breaker but fails gs_04's <=0.50 green signal.
    db_session.add(
        _claim(
            org_id=org_a_id, deal_id=deal.id, attribute="customer_concentration", normalized=0.75
        )
    )
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
    db_session.add(
        _claim(
            org_id=org_a_id, deal_id=deal.id, attribute="customer_concentration", normalized=0.30
        )
    )
    await db_session.flush()

    result = await _evaluate("gs_04", db_session, deal)
    assert result.verdict == "Y"


async def test_gs_04_unknown_when_not_extracted(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id)
    result = await _evaluate("gs_04", db_session, deal)
    assert result.verdict == "unknown"


async def test_db_07_unknown_when_not_extracted(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id)
    result = await _evaluate("db_07", db_session, deal)
    assert result.verdict == "unknown"


# --- gs_06: founder equity post-close (deal field, not a claim) ------------


async def test_gs_06_y_when_equity_at_or_above_threshold(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id, founder_equity_post_close_pct=0.15)
    result = await _evaluate("gs_06", db_session, deal)
    assert result.verdict == "Y"
    assert result.evidence == det_module.DealField("founder_equity_post_close_pct", 0.15)


async def test_gs_06_n_when_equity_below_threshold(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id, founder_equity_post_close_pct=0.05)
    result = await _evaluate("gs_06", db_session, deal)
    assert result.verdict == "N"


async def test_gs_06_unknown_when_unset(db_session, org_a_id):
    deal = await _seed_deal(db_session, org_a_id)
    result = await _evaluate("gs_06", db_session, deal)
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
    assert "ticket #5" in _reason(result)


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
    assert "ticket #5" in _reason(result)


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
