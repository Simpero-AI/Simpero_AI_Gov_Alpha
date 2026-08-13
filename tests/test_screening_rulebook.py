"""Screening #1: pure-Python tests for the Track B rulebook loader.

No Postgres needed -- this only reads a YAML file, unlike the rest of
tests/ which exercise RLS via a real database.
"""

from __future__ import annotations

import pytest

from app.services.screening.rulebook import Rule, load_rulebook

_EXPECTED_IDS = {f"gs_{i:02d}" for i in range(1, 12)} | {f"db_{i:02d}" for i in range(1, 11)}
_VALID_EVALUATORS = {"deterministic", "llm", "external", "hybrid"}


def test_loads_exactly_21_rules() -> None:
    rulebook = load_rulebook()
    assert len(rulebook.rules) == 21


def test_rule_id_set_matches_track_b() -> None:
    rulebook = load_rulebook()
    assert {r.id for r in rulebook.rules} == _EXPECTED_IDS


def test_every_rule_has_a_valid_evaluator() -> None:
    rulebook = load_rulebook()
    for rule in rulebook.rules:
        assert rule.evaluator in _VALID_EVALUATORS, rule.id


def test_unknown_policy_flagged_on_exactly_gs11_and_db08() -> None:
    rulebook = load_rulebook()
    flagged = {r.id for r in rulebook.rules if r.unknown is not None}
    assert flagged == {"gs_11", "db_08"}


def test_version_is_retrievable() -> None:
    rulebook = load_rulebook()
    assert rulebook.version == "track_b.v1"


def test_by_id_lookup() -> None:
    rulebook = load_rulebook()
    rule = rulebook.by_id["gs_03"]
    assert isinstance(rule, Rule)
    assert rule.question == "Company has a paying customer (any revenue)"
    assert rule.threshold == {"revenue_gt": 0}


def test_db_04_prohibited_sectors_come_from_the_rulebook() -> None:
    """The rulebook is the single source of truth for db_04's prohibited
    list -- evaluators (#2) must read it from here, not a hardcoded
    Python constant, or the two would be able to drift."""
    rulebook = load_rulebook()
    rule = rulebook.by_id["db_04"]
    assert rule.threshold == {"in": ["cannabis", "gambling", "crypto_native", "defense_manufacturing"]}


def test_gs_04_and_db_07_share_customer_concentration() -> None:
    rulebook = load_rulebook()
    assert rulebook.by_id["gs_04"].evidence == {"claim": "customer_concentration"}
    assert rulebook.by_id["db_07"].evidence == {"claim": "customer_concentration"}


@pytest.mark.parametrize(
    "bad_yaml,expected_message",
    [
        (
            "version: track_b.v1\ntrack: B\nrules:\n"
            + "\n".join(
                f'  - id: gs_{i:02d}\n    kind: green_signal\n    question: "q"\n'
                f"    evaluator: deterministic\n    evidence: {{claim: revenue}}"
                for i in range(1, 11)
            ),
            "exactly 21 rules",
        ),
        (
            "version: track_b.v1\ntrack: B\nrules:\n"
            '  - id: gs_01\n    kind: green_signal\n    question: "q"\n'
            '    evaluator: not_a_real_evaluator\n    evidence: {claim: revenue}\n'
            + "\n".join(
                f'  - id: {rid}\n    kind: green_signal\n    question: "q"\n'
                f"    evaluator: deterministic\n    evidence: {{claim: revenue}}"
                for rid in sorted(_EXPECTED_IDS - {"gs_01"})
            ),
            "invalid evaluator",
        ),
        (
            "version: track_b.v0\ntrack: B\nrules: []",
            "version must be",
        ),
    ],
    ids=["wrong_count", "bad_evaluator", "wrong_version"],
)
def test_malformed_rulebook_raises(tmp_path, bad_yaml: str, expected_message: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(bad_yaml)
    with pytest.raises(ValueError, match=expected_message):
        load_rulebook(path=path)
