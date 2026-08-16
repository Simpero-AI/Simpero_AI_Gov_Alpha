"""Screening #1: the Track B rulebook as versioned data.

The screener evaluates a deal against 21 Track B rules (the "Must have / deal
breaker" doc). Encoding them as data here -- rather than scattering
if/elif logic across the evaluators -- means the evaluators (#2) and the
decision engine (#4) read one source of truth, and adding a track or
changing a threshold is a YAML edit, not a code change. `version` is
stamped onto every screening result for reproducibility/audit.

Deterministic-first: every rule declares its own `evaluator`. `llm` is used
only for genuinely qualitative criteria; `external` for sanctions/litigation;
`hybrid` gates a deterministic check before ever falling to the model. This
module only loads and validates the rulebook -- it evaluates nothing itself.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

_DEFAULT_PATH = Path(__file__).parent / "rulebooks" / "track_b.yaml"

_EXPECTED_VERSION = "track_b.v1"
_EXPECTED_IDS = frozenset(f"gs_{i:02d}" for i in range(1, 12)) | frozenset(
    f"db_{i:02d}" for i in range(1, 11)
)
# `none` = out of scope for the deterministic CIM-only screener (2026-08-14):
# the rule carries no evaluator and resolves to "No evidence found in the
# documents" (SIM-405/406 descoped). It is a first-class evaluator value, not
# a missing one, so the rulebook still validates and the audit trail can name it.
_VALID_EVALUATORS = frozenset({"deterministic", "llm", "external", "hybrid", "none"})
_VALID_KINDS = frozenset({"green_signal", "deal_breaker"})
# Rules that are structurally unverifiable from the CIM (a negative -- "no
# undisclosed X" -- can never be proven from the document itself) and so
# always resolve to unknown -> human review. Exactly these two carry an
# `unknown` policy note; every other rule must not.
_EXPECTED_UNKNOWN_POLICY_IDS = frozenset({"gs_11", "db_08"})


@dataclass(frozen=True)
class Rule:
    id: str
    track: str
    kind: Literal["green_signal", "deal_breaker"]
    question: str
    evaluator: Literal["deterministic", "llm", "external", "hybrid", "none"]
    evidence: dict
    threshold: dict | None = None
    unknown: str | None = None


@dataclass(frozen=True)
class Rulebook:
    version: str
    rules: tuple[Rule, ...]
    by_id: dict[str, Rule] = field(repr=False)

    def rule(self, rule_id: str) -> Rule:
        return self.by_id[rule_id]


def _validate(version: str, rules: list[Rule]) -> None:
    if version != _EXPECTED_VERSION:
        raise ValueError(f"rulebook version must be {_EXPECTED_VERSION!r}, got {version!r}")

    ids = [r.id for r in rules]
    if len(ids) != len(_EXPECTED_IDS):
        raise ValueError(f"rulebook must have exactly {len(_EXPECTED_IDS)} rules, got {len(ids)}")
    if len(set(ids)) != len(ids):
        raise ValueError("rulebook has duplicate rule ids")
    if set(ids) != _EXPECTED_IDS:
        missing = _EXPECTED_IDS - set(ids)
        extra = set(ids) - _EXPECTED_IDS
        raise ValueError(
            f"rulebook id set mismatch -- missing={missing or None} extra={extra or None}"
        )

    for r in rules:
        if r.evaluator not in _VALID_EVALUATORS:
            raise ValueError(f"rule {r.id!r} has invalid evaluator {r.evaluator!r}")
        if r.kind not in _VALID_KINDS:
            raise ValueError(f"rule {r.id!r} has invalid kind {r.kind!r}")

    unknown_ids = {r.id for r in rules if r.unknown is not None}
    if unknown_ids != _EXPECTED_UNKNOWN_POLICY_IDS:
        raise ValueError(
            "rulebook's unknown-policy rules must be exactly "
            f"{sorted(_EXPECTED_UNKNOWN_POLICY_IDS)}, got {sorted(unknown_ids)}"
        )


@functools.lru_cache(maxsize=1)
def _load_default() -> Rulebook:
    return _load_from_path(_DEFAULT_PATH)


def _load_from_path(path: Path) -> Rulebook:
    raw = yaml.safe_load(path.read_text())
    version = raw["version"]
    rules = [Rule(track=raw["track"], **rule_raw) for rule_raw in raw["rules"]]
    _validate(version, rules)
    return Rulebook(version=version, rules=tuple(rules), by_id={r.id: r for r in rules})


def load_rulebook(path: Path | None = None) -> Rulebook:
    """Load + validate the Track B rulebook.

    The no-argument call is cached (the file never changes at runtime and
    evaluators call this once per rule per screening pass); passing an
    explicit `path` always re-reads and re-validates -- used by tests
    exercising a malformed rulebook.
    """
    if path is None:
        return _load_default()
    return _load_from_path(path)
