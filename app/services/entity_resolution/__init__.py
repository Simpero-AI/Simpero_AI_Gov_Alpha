"""Entity resolution (SIM-262, DS-A-BGCHK-1) -- the front gate of corroboration.

`get_resolver()` is the ONE place that names a concrete registry. Everything
else depends on the `Resolver` protocol, so adding OpenCorporates or Companies
House later is a new class plus one line here -- same containment as
app/services/embedding.py's `get_embedder`.

SEC EDGAR is first and alone deliberately: it is keyless (User-Agent only), so
it needs no provisioning, and CIK is the anchor SIM-408's harvest adapter is
built around.

SIM-420's fold (`resolved.py`) is deliberately NOT re-exported here. It reads
through app/repo/, and app/repo/EntityResolutionRepo.py already imports this
package's `types` -- re-exporting would close that into an import cycle.
Corroboration adapters import it by module:
`from app.services.entity_resolution.resolved import load_resolved_entity`.
"""

from functools import lru_cache

from app.core.config import get_settings
from app.services.entity_resolution.edgar import EdgarResolver
from app.services.entity_resolution.types import (
    EntityResolutionError,
    FormerName,
    MatchedOn,
    Resolution,
    ResolutionStatus,
    Resolver,
)

__all__ = [
    "EdgarResolver",
    "Resolution",
    "EntityResolutionError",
    "FormerName",
    "MatchedOn",
    "ResolutionStatus",
    "Resolver",
    "get_resolver",
]


@lru_cache
def get_resolver() -> Resolver:
    """The configured resolver.

    Not cached across a missing User-Agent: EdgarResolver raises in its own
    constructor when the setting is empty, and lru_cache does not memoize an
    exception, so a later-configured environment still gets a working resolver
    without a process restart.
    """
    settings = get_settings()
    return EdgarResolver(user_agent=settings.sec_edgar_user_agent)
