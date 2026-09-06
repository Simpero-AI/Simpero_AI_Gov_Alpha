"""grant dd_app SELECT, INSERT on chunks (explicit, belt-and-suspenders)

dd_app already inherits full DML on chunks via the doadmin `ALTER DEFAULT
PRIVILEGES` bootstrap (revision "xxxx"), which is why the demo ingest and the
deal-flow ingest work today. This makes that dependency EXPLICIT for the one
table whose population is a new, best-effort write path: start_deal_verification
ingests chunks inside a try/except that swallows a permission error (so a chunk
failure never loses the document's claims). That safety net means a lapse in the
implicit default-privilege grant -- e.g. if ALEMBIC_DATABASE_URL is ever switched
to dd_owner without a matching default-privileges block (the bootstrap's own
comment flags this) -- would silently stop chunk population with only a log line.
An explicit grant guarantees the write regardless.

Additive/idempotent with the default-privilege grant. SELECT + INSERT only:
chunks are append-only and idempotent via ON CONFLICT DO NOTHING, never updated
or deleted by the app.

Revision ID: f4a1c8e2b9d3
Revises: e7b2c9d14a06
Create Date: 2026-09-05 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "f4a1c8e2b9d3"
down_revision: str | Sequence[str] | None = "e7b2c9d14a06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Guarded so this survives a database where `chunks` is absent. On the
    # first-party clean history `chunks` is created by 6c8bc5907f94 far upstream
    # of this migration, so the table exists and the GRANT runs. But a staging DB
    # whose alembic_version was advanced past 6c8bc5907f94 without the physical
    # table (a real staging inconsistency observed 2026-09-06: chunks recorded as
    # applied, table missing) would otherwise fail here with UndefinedTableError
    # and abort the whole deploy. GRANT only when the table exists; dd_app already
    # holds DML on chunks via the doadmin default-privilege bootstrap regardless,
    # so skipping the explicit grant on such a DB costs nothing.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'chunks'
            ) THEN
                GRANT SELECT, INSERT ON chunks TO dd_app;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    # No-op on purpose: a REVOKE here would also strip the SELECT/INSERT dd_app
    # holds via the doadmin default-privilege grant (a plain REVOKE cannot tell
    # the explicit grant from the inherited one), leaving dd_app unable to read
    # or write chunks. This migration only makes an already-present grant
    # explicit, so there is nothing safe to revoke.
    pass
