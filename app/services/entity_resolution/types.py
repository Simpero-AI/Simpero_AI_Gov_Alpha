"""DS-A-BGCHK-1 (SIM-262): the entity-resolution vocabulary and seam.

Resolution is the FRONT GATE of corroboration: it turns a deal's free-text
company name into a stable registry anchor that every downstream check hangs
off (SIM-408's EDGAR harvest, SIM-253's reconcile pass, SIM-254's status
roll-up, SIM-409's undisclosed findings). Everything after it inherits its
answer, which is why the posture here is conservative to the point of being
unhelpful: a wrong anchor poisons every check that follows, and a check
against the wrong company is worse than no check at all.

Three outcomes, and the distinction between the last two is the whole point:

- `resolved`    one confident registry match; `registry_id` carries the anchor.
- `not_found`   we searched and the company genuinely has no filer record.
                Expected for most private, pre-seed targets -- absence is NOT
                contradiction, and this must never read as a negative finding.
- `unresolved`  we could not tell. Ambiguous match, or sources that disagree.
                Nothing was checked and nothing is claimed.

A transport or parse failure is none of the three -- it raises
`EntityResolutionError` and persists nothing. "The registry was unreachable"
and "this company does not exist" are completely different claims, and
collapsing them into one stored status would let an outage read as evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

ResolutionStatus = Literal["resolved", "not_found", "unresolved"]

# Why the query matched. `former_name` is not a lesser match -- an older CIM
# legitimately names the company as it was then (the Facebook -> Meta case) --
# but it IS a materially different fact about the deal, so it is recorded
# rather than flattened into a bare "matched".
MatchedOn = Literal["current_name", "former_name"]


class EntityResolutionError(RuntimeError):
    """The registry was unreachable, returned an unparseable body, or the
    adapter is misconfigured. Deliberately NOT a resolution outcome -- see the
    module docstring."""


@dataclass(frozen=True)
class FormerName:
    """A previous legal name with the window it applied to. The dates are
    load-bearing: they are what lets a later reader tell "this document is old
    and uses the old name" from "this is a different company".

    Both bounds are optional because EDGAR omits `to` on an open range and has
    incomplete history for older filers -- an absent date is unknown, never
    inferred.
    """

    name: str
    from_date: str | None = None
    to_date: str | None = None

    def to_json(self) -> dict:
        return {"name": self.name, "from": self.from_date, "to": self.to_date}


@dataclass(frozen=True)
class Resolution:
    """One resolution attempt against one registry, for one company name.

    `query_name` is stored alongside the answer because `deals.name` is
    mutable: without it, a stored row cannot say what was actually looked up,
    only what the deal happens to be called now.
    """

    status: ResolutionStatus
    source: str
    query_name: str
    registry_id: str | None = None
    legal_name: str | None = None
    former_names: tuple[FormerName, ...] = ()
    matched_on: MatchedOn | None = None
    reason: str | None = None
    evidence: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Mirrors the DB CHECK (ck_entity_resolution_resolved_requires_registry_id)
        # so the invariant fails at construction, in a unit test, rather than
        # only at INSERT time. Same discipline as claims'
        # ck_claims_checked_requires_method: an anchor-less resolve is
        # indistinguishable from a guess.
        if self.status == "resolved" and not self.registry_id:
            raise ValueError("a resolved entity must carry a registry_id")
        if self.status != "resolved" and self.registry_id:
            raise ValueError(
                f"status {self.status!r} must not carry a registry_id -- "
                "only a confident match records an anchor"
            )

    def to_json(self) -> dict:
        """Persisted shape == wire shape, same call as RuleResult.to_json in
        app/services/screening/types.py."""
        return {
            "status": self.status,
            "source": self.source,
            "query_name": self.query_name,
            "registry_id": self.registry_id,
            "legal_name": self.legal_name,
            "former_names": [f.to_json() for f in self.former_names],
            "matched_on": self.matched_on,
            "reason": self.reason,
            "evidence": self.evidence,
        }


class Resolver(Protocol):
    """The swappable seam. Callers depend on THIS, never on a concrete
    registry, so adding OpenCorporates or Companies House later is a new class
    plus one line in `get_resolver` -- same pattern as the `Embedder` protocol
    in app/services/embedding.py.

    Implementations must never raise for a company that simply isn't there:
    that is `not_found`, a real answer. Raise only when the check could not be
    performed at all.
    """

    @property
    def source(self) -> str: ...

    async def resolve(self, name: str) -> Resolution: ...
