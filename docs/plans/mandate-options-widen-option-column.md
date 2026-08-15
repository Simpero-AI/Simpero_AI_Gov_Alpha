# Addendum: widen `mandate_options.option`

Cross-repo addendum requested from the `Simpero_AI_Gov_Web` frontend session
(see that repo's `docs/plans/2026-08-14-mandate-builder-backend-integration.md`,
§8 gap 1). Written 2026-08-15 by that session as a **plan-only** cross-repo
change — no code was written or edited in this repo to produce this doc. To be
implemented by this repo's own Claude Code session.

## Problem

`app/models/mandate.py`'s `MandateOptions.option` is `String(50)`:

```python
option: Mapped[str] = mapped_column(String(50), nullable=False)
```

The Mandate Builder frontend is being wired so "Must Have" and "Deal
Breaker" mandate criteria are entered as full-sentence `MandateOptions` rows
(platform admins create them via the new admin taxonomy page), not free
text. Several of the existing frontend sample criteria — being deleted from
`src/data/mandateDefaults.ts` but illustrative of realistic content — exceed
50 characters:

- `"Founder/CEO with demonstrated execution and scaling experience"` (62 chars)
- `"Regulated verticals: cannabis, gambling, crypto-native, defense"` (63 chars)
- `"Founder unwilling to accept minority structured capital terms"` (60 chars)
- `"Consumer-facing or hardware-dependent business models"` (52 chars)
- `"Gross margins < 50% (services-heavy disguised as SaaS)"` (53 chars)

At 50 chars, every one of these would be rejected or silently truncated.

## Change

Widen `mandate_options.option` from `String(50)` to `String(255)` — enough
headroom for a full mandate-criterion sentence while still bounded (the
column feeds a UNIQUE index on `(category_id, option)`, which stays well
within Postgres' btree row-size limits at 255 chars). Not `Text` — every
other short-label column in this model file (`category` at 150) uses a
bounded `String`, and 255 keeps that convention rather than introducing an
unbounded column for what is still conceptually a label, not a document.

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

    option: Mapped[str] = mapped_column(String(255), nullable=False)  # was String(50)

    __table_args__ = (
        Index("uq_mandate_options_category_option", "category_id", "option", unique=True),
    )
```

Only the `String(50)` → `String(255)` change; nothing else in this model
moves.

### 2. New Alembic migration

At the time this doc was written, `alembic/versions/a1c3e7f2b4d9_mandates.py`
(the migration that creates `mandate_options` in the first place) was
itself still uncommitted, local WIP in this repo. **Verify the actual
current head before writing `down_revision`** — it may still be
`a1c3e7f2b4d9`, or something later if more migrations have landed since.
Do not guess; run `alembic heads` (or equivalent) in this repo's own
session.

```python
"""widen mandate_options.option

Revision ID: <new>
Revises: <current head — verify, likely a1c3e7f2b4d9>
Create Date: ...

"""
from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.alter_column(
        "mandate_options", "option",
        existing_type=sa.String(length=50),
        type_=sa.String(length=255),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "mandate_options", "option",
        existing_type=sa.String(length=255),
        type_=sa.String(length=50),
        existing_nullable=False,
    )
```

If `a1c3e7f2b4d9_mandates.py` is still unapplied/unmerged anywhere, the
simpler and equally correct alternative is to just edit that migration's
`op.create_table("mandate_options", ...)` column definition directly rather
than stacking a second migration on top of one that hasn't shipped —
this repo's own session should judge which applies based on whether that
migration has been applied to any shared/staging database yet.

## Schemas — no change needed

`app/schemas/mandate.py` and `app/schemas/admin/mandate.py`'s
`option: str` fields are unconstrained strings already; no Pydantic
`max_length` was set, so no schema edit is required. (Optional: adding
`max_length=255` to `CreateMandateOptionRequest`/`UpdateMandateOptionRequest`
in `app/schemas/admin/mandate.py` would surface a clean 422 instead of a
raw DB error on overflow — not required by this addendum, worth a
follow-up if picked up.)

## Out of scope

The other two gaps flagged in the frontend plan (§8, gaps 2 and 3 — no
seed data / no stable join key, and duplicate-name writes returning 500
instead of 409) are **not** part of this addendum. They were flagged for
awareness only and were not requested to be fixed.
