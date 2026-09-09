"""deal.sector_raw + deal.hq_geography_raw (display-only stated profile)

The parser's deal_profile classifier grounds a raw sector / HQ in the deal's
materials, but the Path-B reducer (app/services/deal_profile.py) only promotes
that reading to deal.sector / deal.hq_geography when it maps to an approved
mandate option (a "match") or is a determinable off-list value (an "outside").
An "unknown" fit -- most often because the org configured no sector/geo mandate
options to map against -- discards the stated value, so the Company Facts box
shows "Company facts not available" even when the deck plainly states the sector.

These two columns hold the stated (grounded raw) sector / HQ independent of the
mandate fit, for DISPLAY ONLY: build_company_view falls back to them when the
screening column is null. They are NEVER read by a screening evaluator (gs_07 /
gs_08 / db_04 continue to key off deal.sector / deal.hq_geography), so surfacing
a stated-but-unmapped sector can never manufacture a false "not met".

Both nullable, no server_default: legacy deals simply have neither set, same as
sector / hq_geography (revision 565cc5a7589e). New columns inherit the deals
table's grants, so no GRANT is needed here.

Revision ID: c1e5a9f4b207
Revises: b3f8c1a02e7d
Create Date: 2026-09-08 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c1e5a9f4b207"
down_revision: str | Sequence[str] | None = "b3f8c1a02e7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("deals", sa.Column("sector_raw", sa.Text(), nullable=True))
    op.add_column("deals", sa.Column("hq_geography_raw", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("deals", "hq_geography_raw")
    op.drop_column("deals", "sector_raw")
