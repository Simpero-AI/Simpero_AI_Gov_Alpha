# Addendum: nested sub-options on `mandate_options`

Cross-repo addendum requested from the `Simpero_AI_Gov_Web` frontend session (see that repo's `docs/plans/2026-08-15-mandate-suboptions.md`). Written 2026-08-15 by that session as a **plan-only** cross-repo change — no code was written or edited in this repo to produce this doc. To be implemented by this repo's own Claude Code session.

## Problem

The Mandate Builder needs options that have their own child options — first case is Geographies → "Canada" → provinces, with "United States" and every other option having none. Requirement from the user is a **general** capability: any `mandate_options` row, in any category, can have children, with no further schema work when the next category needs it. Only the frontend's drill-down UI is capped at one level; the data model must not be.

Today `mandate_options` is flat: `(id, category_id → mandate_categories ON DELETE CASCADE, option)` with `UNIQUE (category_id, option)`.

## Change

A nullable self-referential `parent_option_id` on `mandate_options`. Parent pointer = arbitrary depth for free, no second table, no path/materialized-tree machinery.

### 1. `app/models/mandate.py`

```python
class MandateOptions(Base):
    __tablename__ = "mandate_options"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )

    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(MandateCategory.id, ondelete="CASCADE"), nullable=False, index=True
    )

    # NULL = top-level option. Non-NULL = sub-option of that row; depth is
    # unbounded by the schema (the Builder UI only renders one level).
    # Sub-options carry their parent's category_id -- denormalized on purpose
    # so the category cascade and every category-scoped query keep working
    # unchanged. Set server-side from the parent row, never from the request
    # body (see app/api/admin/mandates.py), so it cannot diverge.
    parent_option_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mandate_options.id", ondelete="CASCADE"), nullable=True, index=True
    )

    option: Mapped[str] = mapped_column(String(255), nullable=False)

    __table_args__ = (
        Index(
            "uq_mandate_options_category_option", "category_id", "option",
            unique=True, postgresql_where=text("parent_option_id IS NULL"),
        ),
        Index(
            "uq_mandate_options_parent_option", "parent_option_id", "option",
            unique=True, postgresql_where=text("parent_option_id IS NOT NULL"),
        ),
    )
```

**Do not add an ORM `relationship()` for children.** `MandateOptionsRepo.delete` uses `session.delete(obj)`; with a self-referential relationship configured, SQLAlchemy would try to NULL out `parent_option_id` on the children instead of letting Postgres cascade — silently promoting sub-options to top-level rows. If a relationship is ever wanted for convenience, it must carry `passive_deletes=True` and `cascade="all, delete-orphan"`. Nothing in this addendum needs one.

Note this assumes the companion addendum (`mandate-options-widen-option-column.md`, widening `option` to `String(255)`) either has already landed or lands together with this one — the model snippet above already reflects `String(255)`.

### Uniqueness — decided, and it is a real change

The current `UNIQUE (category_id, option)` would forbid Geographies → Canada → "All" **and** Geographies → United States → "All" coexisting. That's wrong: sub-option names are only meaningful relative to their parent. So the single index is replaced by two **partial** indexes:

- top-level rows (`parent_option_id IS NULL`) keep exactly today's rule, unique on `(category_id, option)`;
- child rows (`parent_option_id IS NOT NULL`) are unique on `(parent_option_id, option)` — unique per parent, so different parents may each have "All". `category_id` is redundant there (the parent already implies it).

Partial indexes rather than one `(category_id, parent_option_id, option)` index because NULLs in a plain unique index are never equal to each other, which would silently drop the top-level uniqueness rule. `NULLS NOT DISTINCT` would also work on PG 15+, but partial indexes state the two different rules explicitly and carry no version dependency.

### 2. Alembic migration

At the time this doc was written, `alembic/versions/a1c3e7f2b4d9_mandates.py` (which creates these tables) was still uncommitted local WIP, and no other revision names it as `down_revision` — it is very likely still head, but **verify with `alembic heads` in this repo's own session; do not guess.**

If `a1c3e7f2b4d9` has not been applied to any shared/staging database, the cleaner option is to **fold this into that migration directly** (add the column and swap the index definitions in place) rather than stacking — the same judgment call the `mandate-options-widen-option-column.md` addendum offered, and the one that was evidently taken there (`String(255)` is already in that migration). This repo's session decides based on whether it has shipped anywhere.

Otherwise, a new revision:

```python
def upgrade() -> None:
    op.add_column("mandate_options", sa.Column("parent_option_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_mandate_options_parent_option_id_mandate_options"),
        "mandate_options", "mandate_options",
        ["parent_option_id"], ["id"], ondelete="CASCADE",
    )
    op.create_index(
        op.f("ix_mandate_options_parent_option_id"), "mandate_options", ["parent_option_id"]
    )
    op.drop_index("uq_mandate_options_category_option", table_name="mandate_options")
    op.create_index(
        "uq_mandate_options_category_option", "mandate_options", ["category_id", "option"],
        unique=True, postgresql_where=sa.text("parent_option_id IS NULL"),
    )
    op.create_index(
        "uq_mandate_options_parent_option", "mandate_options", ["parent_option_id", "option"],
        unique=True, postgresql_where=sa.text("parent_option_id IS NOT NULL"),
    )
```

`downgrade()` reverses in the mirror order, deleting child rows first (`DELETE FROM mandate_options WHERE parent_option_id IS NOT NULL`) before restoring the unconditional unique index — otherwise the restore can fail on legitimately-duplicate child names.

**No RLS / grant work.** `mandate_options` is global reference data with no `org_id` and deliberately no RLS policy (per `a1c3e7f2b4d9`'s own comment); DML privileges for `dd_app` come from the existing `ALTER DEFAULT PRIVILEGES` bootstrap. Adding a column changes none of that. The write path stays admin-only via `get_admin_db` + `require_platform_admin`.

### 3. Repo — `app/repo/MandateOptionsRepo.py`

No signature changes needed. `list_all()` / `list_by_category()` continue to return every row including children (unordered w.r.t. depth — the nesting helper below is two-pass and doesn't care). Add nothing speculative; a `list_children(parent_id)` has no caller.

### 4. Schemas

`app/schemas/mandate.py` (product):

```python
class MandateOptionResponse(CamelModel):
    id: str
    option: str
    sub_options: list["MandateOptionResponse"] = []
```

`app/schemas/admin/mandate.py` (admin) — same recursion, keeping the admin surface's existing extra ids:

```python
class MandateOptionResponse(CamelModel):
    id: str
    category_id: str
    parent_option_id: str | None = None
    option: str
    sub_options: list["MandateOptionResponse"] = []
```

Self-referencing models need `MandateOptionResponse.model_rebuild()` after the class body if the forward ref doesn't resolve on its own. Wire keys are camelCase (`subOptions`, `parentOptionId`) via `CamelModel`.

`sub_options` is **always present** in both taxonomy responses, `[]` when there are no children — the frontend branches on `.length`. (The org's saved-mandate JSONB uses the opposite convention, omitting an empty `sub_options`; that blob is opaque to this backend and needs no schema.)

New request schema, admin only:

```python
class CreateMandateSubOptionRequest(CamelModel):
    option: str
```

Deliberately identical to `CreateMandateOptionRequest` but named separately — the parent comes from the path, and reusing the class would invite adding a `parent_option_id` body field later, which is exactly what the endpoint choice below rules out.

### 5. Endpoints

Both routers' `_group_options_by_category` helpers are replaced by one nesting pass. Two-pass, arbitrary depth, ~8 lines, replacing the ~8 lines that are there now:

```
# build response objects for every row, keyed by id
# second pass: if row.parent_option_id -> append to parent's sub_options
#              else -> append to the category bucket
# rows arrive sorted by `option`, so both levels come out alphabetical
```

Guard against a parent id that isn't in the fetched set (can't happen with `list_all()`, can with `list_by_category()` — it can't, since children share the parent's `category_id`; still, treat a missing parent as top-level rather than raising).

**Product `GET /api/mandate-categories`** — same endpoint, options now nested. Purely additive on the wire.

**Admin `GET /api/admin/mandates/categories`** — same nesting, plus `parentOptionId` on each node.

**Admin sub-option create — new route, not a new body field:**

```
POST /api/admin/mandates/options/{parent_option_id}/suboptions
body: { "option": "British Columbia" }  -> 201 MandateOptionResponse
404 if the parent option doesn't exist
```

Chosen over adding an optional `parentOptionId` to `POST /categories/{id}/options` because `category_id` is then **derived from the parent row** — the "parent belongs to a different category than the path" mismatch simply cannot be expressed, so there's no validation branch to write or forget. It also sits in the existing `/options/{id}` path family alongside PATCH and DELETE.

Handler shape mirrors `create_option`: fetch parent via `MandateOptionsRepo.get_by_id`, 404 if `None`, create with `{"category_id": parent.category_id, "parent_option_id": parent.id, "option": payload.option}`, `db.flush()`, audit, return.

**Audit**: reuse the existing `admin_mandate_option_created` event type with `parent_option_id` added to the payload — same table, same operation, no new event type to teach downstream readers.

**`PATCH /options/{id}` and `DELETE /options/{id}` are unchanged** and already correct for sub-options: rename works on any row; delete cascades the whole subtree via the new self-FK (matching how category delete already cascades its options).

## Out of scope

Gaps 2 and 3 from the original frontend plan's §8 (no seed data / no stable `slug` join key; duplicate-name writes returning 500 instead of 409) remain unaddressed. Note that this change makes gap 3 slightly more likely to be hit — two partial unique indexes instead of one — so if it's ever picked up, the `IntegrityError` → 409 handler (precedent: `app/api/deals.py:423`) should cover both index names. Not required here.

No seed rows for Canada's provinces; a platform admin creates them through the admin taxonomy page.
