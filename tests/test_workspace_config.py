"""Screening #3: app/services/screening/workspace_config.py -- the home for
gs_07/gs_08's approved_geographies/approved_sectors, stored by convention
inside the existing InvestmentProfile.mandate JSONB (one row per org)."""

from __future__ import annotations

from app.repo.InvestmentProfileRepo import InvestmentProfileRepo
from app.services.screening.workspace_config import load_workspace_config


async def test_no_investment_profile_row_is_unconfigured(db_session):
    config = await load_workspace_config(db_session)
    assert config.approved_sectors is None
    assert config.approved_geographies is None


async def test_profile_with_no_mandate_is_unconfigured(db_session, org_a_id):
    await InvestmentProfileRepo(db_session).create({"org_id": org_a_id})
    await db_session.flush()

    config = await load_workspace_config(db_session)
    assert config.approved_sectors is None
    assert config.approved_geographies is None


async def test_profile_with_mandate_missing_keys_is_unconfigured(db_session, org_a_id):
    await InvestmentProfileRepo(db_session).create(
        {"org_id": org_a_id, "mandate": {"checkSize": "5-10m"}}
    )
    await db_session.flush()

    config = await load_workspace_config(db_session)
    assert config.approved_sectors is None
    assert config.approved_geographies is None


async def test_explicitly_configured_empty_list_stays_empty_not_none(db_session, org_a_id):
    """A deliberate 'nothing is approved' policy must stay distinguishable
    from 'no policy was ever set' -- the former is a real signal an
    evaluator (#2) can act on, the latter must resolve to `unknown`."""
    await InvestmentProfileRepo(db_session).create(
        {"org_id": org_a_id, "mandate": {"approved_sectors": []}}
    )
    await db_session.flush()

    config = await load_workspace_config(db_session)
    assert config.approved_sectors == []
    assert config.approved_geographies is None


async def test_real_lists_pass_through_unchanged(db_session, org_a_id):
    await InvestmentProfileRepo(db_session).create(
        {
            "org_id": org_a_id,
            "mandate": {
                "approved_sectors": ["saas", "fintech"],
                "approved_geographies": ["US", "CA"],
            },
        }
    )
    await db_session.flush()

    config = await load_workspace_config(db_session)
    assert config.approved_sectors == ["saas", "fintech"]
    assert config.approved_geographies == ["US", "CA"]
