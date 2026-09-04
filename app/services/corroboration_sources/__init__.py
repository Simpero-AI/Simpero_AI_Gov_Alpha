"""External corroboration source adapters (Epic 12).

Each module here implements the CorroborationSource protocol from
app.services.corroboration. DEFAULT_SOURCES is the active registry the
start_deal_corroboration job runs; the adapters are instantiated ONCE here
(process-lifetime) because some cache network data on the instance -- e.g.
SecEdgarSource holds company_tickers.json -- so a per-claim instance would
refetch it and hammer the source's rate limit.

Registration lives HERE, not in app.services.corroboration, to avoid a circular
import: every adapter imports CorroborationVerdict from that module, so it cannot
import the adapters back to fill its list. The corroboration.CORROBORATION_SOURCES
global is populated by slice-assignment (idempotent on re-import) for callers that
read the registry directly; the job imports DEFAULT_SOURCES explicitly.
"""

from app.services.corroboration import CORROBORATION_SOURCES, CorroborationSource
from app.services.corroboration_sources.federal_register import FederalRegisterSource
from app.services.corroboration_sources.ised_corporations import IsedCorporationsSource
from app.services.corroboration_sources.sec_edgar import SecEdgarSource
from app.services.corroboration_sources.trademarks import TrademarkSource

DEFAULT_SOURCES: list[CorroborationSource] = [
    SecEdgarSource(),
    IsedCorporationsSource(),
    FederalRegisterSource(),
    TrademarkSource(),
]

# Idempotent registration (slice-assign, not extend, so re-import doesn't duplicate).
CORROBORATION_SOURCES[:] = DEFAULT_SOURCES

__all__ = ["DEFAULT_SOURCES"]
