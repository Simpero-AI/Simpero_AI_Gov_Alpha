"""Map mandate selections onto rule ids (Path B: mandate-gated screening).

A deal should be screened only on the questions the analyst actually selected,
not the whole rulebook. The Mandate Builder expresses those choices as free-text
options in two admin-managed categories -- Must Have (green-signals the analyst
requires) and Deal Breaker (breakers they enable) -- plus the Geographies /
Target Sectors policies that drive gs_07 / gs_08. This module turns those
selections into the set of rule ids `screen_deal` runs.

The Must-Have / Deal-Breaker options are free text (no code seed, no stored rule
id), so an option is matched to a rule by, in order: the option literally naming
a rule id, the option equalling the rule's canonical `question` (normalized), or
a curated alias. Matching is confined to rules of the matching kind, so a
must-have never resolves to a deal-breaker rule or vice versa. An option that
maps to nothing is logged and dropped -- it simply doesn't turn a question on.
"""

from __future__ import annotations

import logging
from typing import Literal

from app.services.screening.rulebook import Rulebook
from app.services.screening.workspace_config import WorkspaceConfig, normalize_label

logger = logging.getLogger(__name__)

# Curated fallbacks for option wording that is neither a rule id nor the rule's
# `question` verbatim -- keyed by rule id -> accepted *normalized* spellings
# (lower-cased, whitespace-collapsed). Deliberately EMPTY: the live taxonomy's
# Must-Have / Deal-Breaker options are the rulebook questions verbatim (verified
# against staging 2026-08-25), so the id/question matches below cover every real
# option. Add an entry here ONLY for a genuine wording divergence observed in a
# taxonomy -- never a speculative alias, which risks mapping an option to the
# wrong rule.
_ALIASES: dict[str, frozenset[str]] = {}


def map_option_to_rule_id(
    option: str, kind: Literal["green_signal", "deal_breaker"], rulebook: Rulebook
) -> str | None:
    """A selected mandate option -> the rule id it names, or None. Only rules of
    `kind` are considered."""
    key = normalize_label(option)
    if not key:
        return None
    candidates = [r for r in rulebook.rules if r.kind == kind]
    for rule in candidates:  # the option literally names a rule id
        if key == normalize_label(rule.id):
            return rule.id
    for rule in candidates:  # the option is the rule's canonical question
        if key == normalize_label(rule.question):
            return rule.id
    for rule in candidates:  # a curated wording alias
        if key in _ALIASES.get(rule.id, frozenset()):
            return rule.id
    logger.info("mandate option %r did not map to any %s rule; ignored", option, kind)
    return None


def selected_rule_ids(rulebook: Rulebook, config: WorkspaceConfig) -> set[str]:
    """The rule ids a deal should be screened on, given the org's mandate.

    - each Must-Have option -> its green-signal rule
    - each Deal-Breaker option -> its deal-breaker rule
    - gs_07 / gs_08 additionally run whenever the org configured a geography /
      sector policy -- setting those lists IS how those two questions are chosen.

    An empty result means the org selected nothing screenable; `screen_deal`
    treats that as human review, never a vacuous green.
    """
    selected: set[str] = set()
    for option in config.must_have_options or ():
        rule_id = map_option_to_rule_id(option, "green_signal", rulebook)
        if rule_id is not None:
            selected.add(rule_id)
    for option in config.deal_breaker_options or ():
        rule_id = map_option_to_rule_id(option, "deal_breaker", rulebook)
        if rule_id is not None:
            selected.add(rule_id)
    if config.approved_geographies is not None:
        selected.add("gs_07")
    if config.approved_sectors is not None:
        selected.add("gs_08")
    return selected
