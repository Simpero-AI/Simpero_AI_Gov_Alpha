"""AE-A-CORR (SIM-420): the deal-scoped resolved-entity artifact.

Corroboration adapters must not key on `claim.entity`. That string is whatever
a deck happened to print -- "Acme", "Acme Inc.", "Acme Technologies Ltd." -- and
looking a registry up by it produces the two failures that matter most here:

- a **common-name false positive**, where a big registry returns a different
  company that happens to share the name, and the adapter then "corroborates"
  the wrong entity. `conflicted` is sticky, so a false conflict is not merely
  noise; it is unrecoverable without a human;
- a **false miss on a former name**, where an older deck names the company as
  it was then and the adapter reads the rename as a different company.

So adapters key on THIS instead: one deal-scoped artifact carrying the
canonical legal name, every alias/former name the registries know, and the
registry cross-refs already resolved (SEC CIK, ISED corporationId, BC
registration number). It is folded ONCE per deal from the per-registry
`entity_resolution` attempts (SIM-262) and read from a session-scoped cache, so
a pass over N claims x M sources folds it once, not N x M times.

Reachable inside a source's `check()` from `db` + `claim.deal_id` alone, which
is why **no CorroborationSource protocol change is needed** for any of this.

`load_resolved_entity` returning None is the clean no-signal path: no registry
resolved this deal, so an adapter has nothing to compare against and must
return None rather than a verdict. Absence is never a conflict.

Deterministic end to end. The AI-propose seam the handover permits at this edge
(edge 1: proposing candidate names to search) sits UPSTREAM, in the resolvers
that write `entity_resolution` rows -- nothing here is model-derived, and the
fold below is a pure function of the rows it reads.

Two names, one concept: `DealEntity` (this module) is the in-memory form that
adapters use; `app.models.resolved_entity.ResolvedEntity` is the row it is
loaded from and written to. Same split, same reasons, as `Resolution` vs
`EntityResolution` in this package.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entity_resolution import EntityResolution
from app.models.resolved_entity import (
    REGISTRY_BC_REGISTRATION_NUMBER,
    REGISTRY_CIK,
    REGISTRY_ISED_CORPORATION_ID,
    REGISTRY_KEYS,
)
from app.repo.EntityResolutionRepo import EntityResolutionRepo
from app.repo.ResolvedEntityRepo import ResolvedEntityRepo

# Which registry-id key each `entity_resolution.source` contributes, in order of
# how authoritative that source's LEGAL NAME is for the target book (Canadian
# pre-seed): the federal Canadian register first, the provincial one next, SEC
# last -- a Canadian startup's SEC footprint, where it has one at all, is
# usually a US subsidiary filing under a different name, so its legal name is
# the least likely to be the one the deck means.
#
# Order is load-bearing, not cosmetic: it is what makes the fold deterministic.
# Two folds of the same rows must pick the same canonical name, or the same
# registry lookup could agree today and conflict tomorrow.
#
# A source not listed here contributes its names but NO registry id -- there is
# no key to file it under, and inventing one would hand adapters an id whose
# registry they cannot know. Adding a registry is one line here plus one
# constant in app/models/resolved_entity.py.
_SOURCE_REGISTRY: tuple[tuple[str, str], ...] = (
    ("ised", REGISTRY_ISED_CORPORATION_ID),
    ("orgbook_bc", REGISTRY_BC_REGISTRATION_NUMBER),
    ("sec_edgar", REGISTRY_CIK),
)
_REGISTRY_KEY_BY_SOURCE: Mapping[str, str] = dict(_SOURCE_REGISTRY)
_SOURCE_RANK: Mapping[str, int] = {source: i for i, (source, _) in enumerate(_SOURCE_REGISTRY)}

# Only a `resolved` attempt carries an anchor -- entity_resolution's own CHECK
# guarantees registry_id is NULL for every other status -- so the fold reads
# nothing else. `not_found` is a real and useful row on that table ("we looked,
# SEC has nothing"), but it contributes no identity here.
_RESOLVED = "resolved"

# Punctuation and separators, not letters: `\W` is unicode-aware, so accented
# characters survive this and are folded separately below.
_SEPARATORS = re.compile(r"[\W_]+", flags=re.UNICODE)


def normalize_name(name: str) -> str:
    """The comparison form of a company name: accent-folded, case-folded,
    punctuation-stripped, whitespace-collapsed.

    Deliberately an EQUIVALENCE, not a fuzzy match. It closes exactly the gaps
    where two registries spell the same legal name differently -- "Acme Inc." vs
    "ACME INC", "Societe Generale" vs "Societe Generale" with accents -- and
    closes nothing else. In particular it does NOT strip legal suffixes: "Acme
    Inc." and "Acme Holdings Inc." stay different companies, because they
    usually are, and a match here is what licenses an adapter to raise a
    conflict.
    """
    # NFKD then drop combining marks: registries disagree on accents far more
    # often than they disagree on the letters underneath them.
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(_SEPARATORS.sub(" ", stripped.casefold()).split())


@dataclass(frozen=True)
class DealEntity:
    """One deal's resolved identity -- the artifact adapters match against.

    `aliases` holds former and alternate legal names with the canonical one
    excluded; `registry_ids` holds only the registries that actually answered,
    keyed by REGISTRY_KEYS.
    """

    deal_id: uuid.UUID
    canonical_name: str
    aliases: tuple[str, ...] = ()
    registry_ids: Mapping[str, str] = field(default_factory=dict)

    @property
    def names(self) -> tuple[str, ...]:
        """Every name this company is known by, canonical first."""
        return (self.canonical_name, *self.aliases)

    def matches(self, candidate: str | None) -> str | None:
        """The known name `candidate` is the same name as, or None.

        Returns the matched name rather than a bool so an adapter can record
        WHICH name matched -- "the registry's current name" and "a former name
        this deck still uses" are materially different facts about the deal, and
        flattening them into True would throw that away (same reasoning as
        `Resolution.matched_on`).

        Canonical is checked first, so a name that is both canonical and an
        alias reports as canonical.
        """
        if not candidate:
            return None
        target = normalize_name(candidate)
        if not target:
            return None
        for known in self.names:
            if normalize_name(known) == target:
                return known
        return None

    def registry_id(self, registry: str) -> str | None:
        """This entity's id in `registry` (one of REGISTRY_KEYS), or None when
        that registry has not resolved it. Unknown keys raise rather than
        returning None: a typo'd key would otherwise read as "this registry has
        no id for the company", silently disabling the adapter that made it."""
        if registry not in REGISTRY_KEYS:
            raise ValueError(f"unknown registry {registry!r}; expected one of {REGISTRY_KEYS}")
        return self.registry_ids.get(registry)

    def to_json(self) -> dict:
        return {
            "deal_id": str(self.deal_id),
            "canonical_name": self.canonical_name,
            "aliases": list(self.aliases),
            "registry_ids": dict(self.registry_ids),
        }


def _former_names(row: EntityResolution) -> list[str]:
    """The `name` of each former-name entry, tolerating a row whose JSONB is
    absent or not the expected shape -- a malformed history must not sink the
    fold, it just contributes no alias."""
    names: list[str] = []
    for entry in row.former_names or []:
        if isinstance(entry, Mapping):
            name = entry.get("name")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    return names


def fold_resolutions(
    deal_id: uuid.UUID, resolutions: Iterable[EntityResolution]
) -> DealEntity | None:
    """Fold this deal's per-registry attempts into one identity, or None when
    none of them resolved.

    None is the whole no-signal contract in one return value: nothing resolved,
    so there is no anchor, so no `resolved_entity` row is written and every
    adapter downstream returns None rather than a verdict. A name-only artifact
    with no registry id would be worse than nothing -- it looks like an anchor
    and is not one.

    Pure and total: same rows in, same identity out, no I/O.
    """
    resolved = [r for r in resolutions if r.status == _RESOLVED and r.registry_id and r.legal_name]
    if not resolved:
        return None

    # Deterministic: registry precedence first, then the newer attempt, then the
    # id as a pure stability tiebreak. created_at can be None on a row that has
    # not been flushed yet (server_default), so sort those last rather than
    # comparing None to a datetime.
    ordered = sorted(
        resolved,
        key=lambda r: (
            _SOURCE_RANK.get(r.source, len(_SOURCE_RANK)),
            r.created_at is None,
            -(r.created_at.timestamp() if r.created_at is not None else 0.0),
            str(r.id or ""),
        ),
    )

    canonical = ordered[0].legal_name
    if not canonical:  # unreachable: filtered above. Kept so the type narrows.
        return None

    # Dedupe on the NORMALIZED name while keeping the original spelling of the
    # first occurrence: "ACME INC" and "Acme Inc." are one alias, not two, and
    # the reader should see the spelling a registry actually returned.
    # Membership in the seen set, never a length comparison -- two different
    # spellings can normalize to the same key, so counting would report the
    # wrong one as new.
    seen = {normalize_name(canonical)}
    aliases: list[str] = []
    for row in ordered:
        for name in (row.legal_name, *_former_names(row)):
            if not name:
                continue
            key = normalize_name(name)
            if not key or key in seen:
                continue
            seen.add(key)
            aliases.append(name)

    # First writer per registry wins, which under the ordering above is the
    # newest attempt from that registry -- an older superseded lookup never
    # overwrites the one that replaced it.
    registry_ids: dict[str, str] = {}
    for row in ordered:
        key = _REGISTRY_KEY_BY_SOURCE.get(row.source)
        if key is None or key in registry_ids or not row.registry_id:
            continue
        registry_ids[key] = row.registry_id

    if not registry_ids:
        # Every contributing row came from a source with no registry key of its
        # own. Same call as "nothing resolved": no anchor, no artifact.
        return None

    return DealEntity(
        deal_id=deal_id,
        canonical_name=canonical,
        aliases=tuple(aliases),
        registry_ids=registry_ids,
    )


def _evidence(entity: DealEntity, rows: Sequence[EntityResolution]) -> dict:
    """What the fold saw, so a reader can retrace it without re-querying."""
    return {
        "folded_from": [
            {
                "entity_resolution_id": str(r.id) if r.id else None,
                "source": r.source,
                "status": r.status,
                "registry_id": r.registry_id,
                "legal_name": r.legal_name,
                "matched_on": r.matched_on,
            }
            for r in rows
        ],
        "resolved_entity": entity.to_json(),
    }


# Session-scoped memo of the fold, keyed by deal. `AsyncSession.info` is a plain
# per-session dict, which is exactly the right lifetime: a verification pass
# holds one session, so the entry lives for that pass and dies with it. It can
# neither leak across tenants (a session is already SET LOCAL-scoped to one org)
# nor go stale across runs (the next pass opens a new session).
_CACHE_KEY = "sim420_resolved_entity_by_deal"


def _cache(db: AsyncSession) -> dict[uuid.UUID, DealEntity | None]:
    return db.info.setdefault(_CACHE_KEY, {})


async def load_resolved_entity(db: AsyncSession, deal_id: uuid.UUID) -> DealEntity | None:
    """This deal's resolved identity, or None when nothing has resolved it.

    THE entry point for corroboration adapters: reachable inside `check()` from
    `db` + `claim.deal_id`, so no CorroborationSource protocol change is needed.

    Resolves once per deal, not once per (claim x source). The result -- None
    included -- is memoized on the session, so a pass over 200 claims and 4
    sources issues ONE query for the deal instead of 800. Caching the None
    matters as much as caching the hit: the common case in the target book is a
    pre-seed company no registry has resolved, and that case must not cost a
    query per claim per adapter.

    `db` must already be RLS-scoped by the caller, same contract as the rest of
    app/services/.
    """
    cache = _cache(db)
    if deal_id in cache:
        return cache[deal_id]

    row = await ResolvedEntityRepo(db).latest_for_deal(deal_id)
    entity = (
        None
        if row is None
        else DealEntity(
            deal_id=row.deal_id,
            canonical_name=row.canonical_name,
            aliases=tuple(n for n in (row.aliases or []) if isinstance(n, str)),
            registry_ids={k: v for k, v in (row.registry_ids or {}).items() if isinstance(v, str)},
        )
    )
    cache[deal_id] = entity
    return entity


async def record_resolved_entity(
    db: AsyncSession, *, org_id: int, deal_id: uuid.UUID
) -> DealEntity | None:
    """Fold this deal's `entity_resolution` attempts into one `resolved_entity`
    row and return the identity, or None when nothing resolved (in which case
    NOTHING is written -- see `fold_resolutions`).

    Append-only, so two callers racing on the same deal append two rows rather
    than losing one another's write, and `latest_for_deal` picks the newer
    deterministically (clock_timestamp advances even inside one transaction).
    There is no read-then-update here to lose.

    Primes the session cache with the fold it just wrote, so a caller that
    records and then runs the corroboration pass on the same session does not
    re-read its own write.

    Does not flush or commit; the caller flushes, same contract as the rest of
    app/services/.
    """
    rows = await EntityResolutionRepo(db).latest_per_source_for_deal(deal_id)
    entity = fold_resolutions(deal_id, rows)
    if entity is None:
        _cache(db)[deal_id] = None
        return None

    await ResolvedEntityRepo(db).create(
        {
            "org_id": org_id,
            "deal_id": deal_id,
            "canonical_name": entity.canonical_name,
            "aliases": list(entity.aliases),
            "registry_ids": dict(entity.registry_ids),
            "evidence": _evidence(entity, rows),
        }
    )
    _cache(db)[deal_id] = entity
    return entity
