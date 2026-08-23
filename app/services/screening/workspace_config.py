"""Screening #3: gs_07/gs_08's approved geography/sector lists, derived from
the org's own mandate.

The source of truth is the `mandates` row the firm actually edits in the
Mandate Builder (PUT /api/mandate -- app/api/mandates.py). Screening reads
that same row rather than a second, screening-only copy: until SIM-414 this
module read `investment_profiles.mandate`, a table with no writer anywhere in
the app, so gs_07/gs_08 resolved to `unknown` for every deal of every org no
matter what the firm had selected.

Four things about the stored blob drive the shape of this module:

* **Categories are matched by stable identity first, name last.** Every saved
  entry carries `category_id` (the frontend's toMandateItems() has always
  attached it -- see _mandate_item_key in app/api/mandates.py), and
  mandate_categories carries an immutable, backend-owned `slug`. Both are
  preferred over the `category` display name, which is admin-editable and so
  is the one key that can silently stop matching after a rename. The display
  name is still accepted as a last resort, for entries saved before the id was
  flowing and for categories an admin created outside the slug enum.
* **The blob is never validated on the way in** (`UpsertMandateRequest
  .mandate: list[Any]`), so every read here is defensive. Nothing in this
  module raises on a malformed blob: a screening run must not fail the whole
  analysis_run because one mandate entry is the wrong shape, and `RuleResult`
  already has an honest channel -- `unknown` -- for "we could not tell".
* **`sub_options` is omitted when empty.** A selected option carrying no
  `sub_options` means "all of it": it approves itself and its whole subtree
  from the taxonomy (Canada => Ontario, BC, ...). A selected option carrying
  `sub_options` approves itself plus only those children (Canada > BC => not
  Ontario). Because omitted and `[]` are indistinguishable in a saved blob,
  `[]` is read as omitted.
* **The check-size entry has no `options` key at all** (it carries min/max),
  so "no options list" is a normal shape here, not a malformed one.

Option matching is deliberately fold-only -- case, whitespace and Unicode form
-- with **no alias table**: "US" is not "United States". Aliasing would
hardcode a vocabulary in application code that the deal form and the
admin-managed taxonomy don't share, and it would go stale the moment an admin
renames an option. The durable fix is for the deal form and the taxonomy to
share one vocabulary.

Cost: gs_07 and gs_08 each load this independently -- 2 loads per screening
run, 1 SELECT each when the org has no mandate (the early return below), 3
each when it does. Deliberately not cached: this is per-org data read on an
RLS-scoped session, so a process-level cache would be a cross-tenant leak, and
a session-keyed one would rest on "one session, one org" -- a convention in
this codebase (see admin_dependencies._set_org_scope), not an invariant. If it
ever matters, thread one config through `screen_deal` instead.
"""

from __future__ import annotations

import logging
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mandate import MandateCategory, MandateOptions
from app.repo.MandateCategoryRepo import MandateCategoryRepo
from app.repo.MandateOptionsRepo import MandateOptionsRepo
from app.repo.MandateRepo import MandateRepo

logger = logging.getLogger(__name__)


def normalize_label(value: str) -> str:
    """The one matching rule, applied to BOTH sides of every comparison:
    NFKC-normalize, collapse all runs of whitespace to a single space, strip,
    casefold. No synonym/alias expansion, on purpose (see module docstring)."""
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


@dataclass(frozen=True)
class _CategoryTarget:
    """One of the two mandate categories screening reads, and every way a
    saved entry or a taxonomy row can be recognized as it."""

    # Mirrors MandateCategorySlug in app/schemas/admin/mandate.py by VALUE, not
    # by import: that enum is an admin-portal schema, and admin and product
    # code do not share modules (CLAUDE.md). tests/test_workspace_config.py
    # pins these two strings against the enum so the copy can't drift.
    slug: str
    # Only consulted for rows/entries with no slug -- an admin can create a
    # category outside the fixed enum, and legacy entries predate the id.
    name_aliases: frozenset[str]


GEOGRAPHY = _CategoryTarget(
    slug="geographies",
    name_aliases=frozenset({"geographies", "geography"}),
)
SECTOR = _CategoryTarget(
    slug="target_sectors",
    # Canonical label is "Target Sectors"; the bare spellings are accepted for
    # environments whose taxonomy was hand-created before the slug enum.
    name_aliases=frozenset({"target sectors", "target sector", "sectors", "sector"}),
)


@dataclass(frozen=True)
class WorkspaceConfig:
    # None = the org has never configured this policy -- evaluators must treat
    # that as `unknown`, not as "nothing is approved" (a rule can't fire
    # against a policy that was never set).
    #
    # The discriminator is per-category, and it is "did the mandate yield any
    # usable option for this category", NOT "does the org have a mandates row":
    # a firm that filled in sectors and never opened geographies has genuinely
    # not set a geography policy, and resolving that to `N` would auto-fail
    # every deal against a policy nobody wrote. An empty-but-present category
    # ({"category": "Geographies", "options": []}) reads as None for the same
    # reason -- in this blob it is indistinguishable from "the Builder rendered
    # the category and nothing was ticked".
    #
    # The lists hold the RAW display strings: they are what a human reads in a
    # log or a test assertion. Matching goes through the normalized key sets
    # below instead -- use approves_*(), never a bare `in`.
    approved_sectors: list[str] | None
    approved_geographies: list[str] | None

    _sector_keys: frozenset[str] = field(init=False, repr=False, compare=False, default=frozenset())
    _geography_keys: frozenset[str] = field(
        init=False, repr=False, compare=False, default=frozenset()
    )

    def __post_init__(self) -> None:
        # object.__setattr__ on a frozen dataclass -- same idiom as
        # RuleResult.__post_init__ in app/services/screening/types.py.
        object.__setattr__(
            self, "_sector_keys", frozenset(map(normalize_label, self.approved_sectors or ()))
        )
        object.__setattr__(
            self,
            "_geography_keys",
            frozenset(map(normalize_label, self.approved_geographies or ())),
        )

    def approves_sector(self, value: str) -> bool:
        """False when the policy is unset -- callers that need to tell the two
        apart must check `approved_sectors is None` first (the evaluators do,
        to emit a distinct `unknown` reason)."""
        return normalize_label(value) in self._sector_keys

    def approves_geography(self, value: str) -> bool:
        return normalize_label(value) in self._geography_keys


@dataclass(frozen=True)
class _CategoryIndex:
    """One taxonomy category, indexed for both join keys a saved option can
    carry: the option_id it stored, and -- when that id is missing or points at
    a row an admin has since deleted -- the option's name."""

    children: dict[uuid.UUID, list[uuid.UUID]]
    labels: dict[uuid.UUID, str]
    by_name: dict[str, uuid.UUID]


_EMPTY_INDEX = _CategoryIndex({}, {}, {})


def _is_category(row: MandateCategory, target: _CategoryTarget) -> bool:
    """Slug is authoritative when the row has one: a category explicitly
    created as `investment_stage` is not a sector category however it was
    later renamed. Only slug-less rows fall back to the display name."""
    if row.slug is not None:
        return row.slug == target.slug
    return normalize_label(row.category) in target.name_aliases


def _entry_matches(entry: dict[str, Any], target: _CategoryTarget, ids: set[str]) -> bool:
    """A saved entry -> one of the two categories, by the same key precedence
    _mandate_item_key uses: category_id, then slug, then the display name."""
    category_id = entry.get("category_id")
    if isinstance(category_id, str) and category_id:
        # An id that resolves to a DIFFERENT category is a definite answer, not
        # a reason to keep guessing by name.
        return category_id in ids

    slug = entry.get("slug")
    if isinstance(slug, str) and slug:
        return slug == target.slug

    category = entry.get("category")
    return isinstance(category, str) and normalize_label(category) in target.name_aliases


def _matching_entries(blob: Any, target: _CategoryTarget, ids: set[str]) -> list[dict[str, Any]]:
    """Every entry in the saved blob belonging to `target`. Pure -- no DB. More
    than one can match (a taxonomy carrying both "Sectors" and "Target
    Sectors"); they are unioned, not overwritten."""
    if not isinstance(blob, list):
        return []

    matched: list[dict[str, Any]] = []
    for entry in blob:
        if not isinstance(entry, dict):
            logger.warning("mandate blob entry is not an object, skipping: %r", type(entry))
            continue
        if _entry_matches(entry, target, ids):
            matched.append(entry)
    return matched


def _option_id(item: dict[str, Any]) -> uuid.UUID | None:
    raw = item.get("option_id")
    if not isinstance(raw, str):
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        logger.warning("mandate option_id is not a UUID, falling back to name: %r", raw)
        return None


def _label_of(item: Any) -> str | None:
    if not isinstance(item, dict):
        logger.warning("mandate option is not an object, skipping: %r", type(item))
        return None
    label = item.get("option")
    if not isinstance(label, str) or not label.strip():
        logger.warning("mandate option carries no usable `option` string, skipping")
        return None
    return label


def _resolve(item: dict[str, Any], label: str, index: _CategoryIndex) -> uuid.UUID | None:
    """A saved option -> its taxonomy node, by id first then by name. None when
    the option isn't in the taxonomy at all (renamed or deleted since the
    mandate was saved) -- the literal string is still approved, it just can't
    be expanded."""
    node_id = _option_id(item)
    if node_id is not None and node_id in index.labels:
        return node_id
    return index.by_name.get(normalize_label(label))


def _expand_subtree(
    node_id: uuid.UUID,
    index: _CategoryIndex,
    add: Callable[[str], None],
    seen: set[uuid.UUID],
) -> None:
    """Every descendant of `node_id`. `seen` guards against a cycle in
    parent_option_id -- the self-FK permits one, and an infinite loop inside a
    background job is a far nastier failure than a skipped node."""
    for child_id in index.children.get(node_id, []):
        if child_id in seen:
            continue
        seen.add(child_id)
        label = index.labels.get(child_id)
        if label is not None:
            add(label)
        _expand_subtree(child_id, index, add, seen)


def _add_listed_children(
    sub_options: Sequence[Any],
    parent_id: uuid.UUID,
    index: _CategoryIndex,
    add: Callable[[str], None],
) -> None:
    """Only the sub-options the firm actually ticked. Each is then treated by
    the same rule as a top-level pick -- a ticked child that lists no children
    of its own approves its whole subtree. The taxonomy allows unbounded depth
    even though the Builder UI renders one level."""
    by_key: dict[str, uuid.UUID] = {}
    for child_id in index.children.get(parent_id, []):
        child_label = index.labels.get(child_id)
        if child_label is not None:
            by_key.setdefault(normalize_label(child_label), child_id)

    for item in sub_options:
        label = _label_of(item)
        if label is None:
            continue
        add(label)
        node_id = _option_id(item)
        if node_id is None or node_id not in index.labels:
            node_id = by_key.get(normalize_label(label))
        if node_id is not None:
            _expand_subtree(node_id, index, add, set())


def _approved_labels(entries: list[dict[str, Any]], index: _CategoryIndex) -> list[str] | None:
    """The whole transform: this category's saved picks -> the approved display
    strings, sub-options expanded. Pure -- no DB.

    None means "no usable policy here" (see the WorkspaceConfig comment). Every
    malformed shape is skipped rather than raised on."""
    labels: list[str] = []
    seen_keys: set[str] = set()

    def add(raw: str) -> None:
        key = normalize_label(raw)
        if not key or key in seen_keys:
            return  # blank, or the same option reached twice
        seen_keys.add(key)
        labels.append(raw)

    for entry in entries:
        options = entry.get("options")
        if not isinstance(options, list):
            # The legitimate shape here is the check-size entry, which carries
            # min/max and no `options` key at all.
            if "options" in entry:
                logger.warning("mandate category %r has non-list options", entry.get("category"))
            continue

        for item in options:
            label = _label_of(item)
            if label is None:
                continue
            add(label)

            node_id = _resolve(item, label, index)
            if node_id is None:
                continue  # not in the taxonomy -- literal match only

            sub_options = item.get("sub_options")
            if isinstance(sub_options, list) and sub_options:
                _add_listed_children(sub_options, node_id, index, add)
            else:
                # Omitted, [], or malformed -- "all of it". Saved blobs omit
                # the key when empty, so [] cannot mean anything else.
                if sub_options is not None and not isinstance(sub_options, list):
                    logger.warning("mandate option %r has non-list sub_options", label)
                _expand_subtree(node_id, index, add, set())

    return labels or None


def _build_index(rows: Iterable[MandateOptions]) -> _CategoryIndex:
    children: dict[uuid.UUID, list[uuid.UUID]] = {}
    labels: dict[uuid.UUID, str] = {}
    by_name: dict[str, uuid.UUID] = {}

    for row in rows:
        labels[row.id] = row.option
        # Top-level names are unique per category and child names unique per
        # parent (the two partial indexes on mandate_options), so two parents
        # can each own a child named e.g. "All" -- first one wins for the name
        # fallback, which only ever runs for a blob that lost its option_id.
        by_name.setdefault(normalize_label(row.option), row.id)
        if row.parent_option_id is not None:
            children.setdefault(row.parent_option_id, []).append(row.id)

    return _CategoryIndex(children=children, labels=labels, by_name=by_name)


def _merge(indexes: list[_CategoryIndex]) -> _CategoryIndex:
    """Union of several taxonomy categories that all resolve to the same
    target, so an environment carrying both "Sectors" and "Target Sectors"
    reads as their union rather than whichever one sorted first."""
    if not indexes:
        return _EMPTY_INDEX
    if len(indexes) == 1:
        return indexes[0]

    children: dict[uuid.UUID, list[uuid.UUID]] = {}
    labels: dict[uuid.UUID, str] = {}
    by_name: dict[str, uuid.UUID] = {}
    for index in indexes:
        for parent_id, child_ids in index.children.items():
            children.setdefault(parent_id, []).extend(child_ids)
        labels.update(index.labels)
        for name, node_id in index.by_name.items():
            by_name.setdefault(name, node_id)
    return _CategoryIndex(children=children, labels=labels, by_name=by_name)


async def _load_targets(
    session: AsyncSession,
) -> dict[str, tuple[set[str], _CategoryIndex]]:
    """The two categories screening reads -> (their ids, their option index).
    Two SELECTs against the global (un-RLS'd) reference tables."""
    categories = await MandateCategoryRepo(session).list()
    resolved = {
        target.slug: [row for row in categories if _is_category(row, target)]
        for target in (GEOGRAPHY, SECTOR)
    }

    wanted_ids = [row.id for rows in resolved.values() for row in rows]
    options = await MandateOptionsRepo(session).list_by_categories(wanted_ids)
    by_category: dict[uuid.UUID, list[MandateOptions]] = {}
    for option in options:
        by_category.setdefault(option.category_id, []).append(option)

    return {
        slug: (
            {str(row.id) for row in rows},
            _merge([_build_index(by_category.get(row.id, [])) for row in rows]),
        )
        for slug, rows in resolved.items()
    }


async def load_workspace_config(session: AsyncSession) -> WorkspaceConfig:
    """`session` must already be RLS-scoped (SET LOCAL app.org_id) by the
    caller, same contract as the rest of app/services/."""
    mandate = await MandateRepo(session).get_for_org()
    blob = mandate.mandate if mandate is not None else None
    if not isinstance(blob, list) or not blob:
        # No mandate saved at all -- both policies unset, and the taxonomy
        # never gets read. This is the common path today, and it costs the same
        # single SELECT the pre-SIM-414 implementation did.
        return WorkspaceConfig(approved_sectors=None, approved_geographies=None)

    targets = await _load_targets(session)
    geography_ids, geography_index = targets[GEOGRAPHY.slug]
    sector_ids, sector_index = targets[SECTOR.slug]

    return WorkspaceConfig(
        approved_sectors=_approved_labels(
            _matching_entries(blob, SECTOR, sector_ids), sector_index
        ),
        approved_geographies=_approved_labels(
            _matching_entries(blob, GEOGRAPHY, geography_ids), geography_index
        ),
    )
