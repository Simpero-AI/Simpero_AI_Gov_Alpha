"""Screening #3: the workspace-config home for gs_07/gs_08's approved lists.

The rulebook (app/services/screening/rulebooks/track_b.yaml) references
`approved_geographies`/`approved_sectors` by name -- they vary per org/
mandate, unlike db_04's prohibited-sector list, which is fixed and lives
directly in the rulebook. No standalone "workspace config" concept exists
in this codebase; rather than add a new table, this reuses
`InvestmentProfile.mandate` (one row per org, JSONB, already RLS-scoped)
by convention, storing the two lists as keys inside it.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.repo.InvestmentProfileRepo import InvestmentProfileRepo


@dataclass(frozen=True)
class WorkspaceConfig:
    # None = the org has never configured this policy -- evaluators (#2)
    # must treat that as `unknown`, not as "nothing is approved" (a rule
    # can't fire against a policy that was never set). An explicitly
    # configured empty list ([]) is a real, deliberate policy and stays
    # distinguishable from None for exactly that reason.
    approved_sectors: list[str] | None
    approved_geographies: list[str] | None


async def load_workspace_config(session: AsyncSession) -> WorkspaceConfig:
    """`session` must already be RLS-scoped (SET LOCAL app.org_id) by the
    caller, same contract as the rest of app/services/."""
    profile = await InvestmentProfileRepo(session).get_for_org()
    if profile is None:
        return WorkspaceConfig(approved_sectors=None, approved_geographies=None)

    mandate = profile.mandate or {}
    return WorkspaceConfig(
        approved_sectors=mandate.get("approved_sectors"),
        approved_geographies=mandate.get("approved_geographies"),
    )
