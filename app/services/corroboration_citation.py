"""Resolve a corroboration event to a clickable citation URL for the external
record it checked against -- the "cite the cite" of the corroboration display.

An event's `result` is the source's raw finding, kept verbatim, and its shape
differs per source (see corroboration_sources/*). Only the US Federal Register
source stores a ready human URL; the others store a stable external identifier
(an SEC CIK, an ISED/OrgBook registration id) from which the record's canonical
URL is built here, reusing each source's own registry host so the link points at
exactly what the check queried -- never a guessed URL. A source with no reliable
per-record permalink (the trademark registers expose search endpoints only, and
the stored id can be a registration OR an application/serial number) returns
None; the identifier is then shown as plain text rather than a dead link.
"""

from collections.abc import Mapping
from typing import Any


def corroboration_source_url(outside_source: str, result: Mapping[str, Any]) -> str | None:
    """The external record URL for one corroboration event, or None when the
    source exposes no stable per-record link (the caller then surfaces the raw
    identifier from `result` instead)."""
    if outside_source == "us_federal_register":
        document = result.get("document")
        if isinstance(document, Mapping):
            url = document.get("html_url")
            if isinstance(url, str) and url:
                return url
        return None

    if outside_source == "sec_edgar":
        cik = _as_int(result.get("cik"))
        if cik is not None:
            # EDGAR's canonical company page for a CIK (zero-padded to 10), all
            # filing types. The adapter itself keys off the companyfacts API for
            # the same CIK; this is the human-readable face of that filer.
            return (
                "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                f"&CIK={cik:010d}&type=&dateb=&owner=include&count=40"
            )
        return None

    if outside_source == "ised_corporations_canada":
        registry = result.get("registry")
        registry_id = result.get("registry_id")
        if not isinstance(registry_id, str) or not registry_id:
            return None
        if registry == "orgbook_bc":
            # OrgBook BC's human-facing entity page.
            return f"https://orgbook.gov.bc.ca/entity/{registry_id}"
        if registry == "ised":
            # Corporations Canada serves the record via its API host only; this
            # is the exact record the adapter fetched (a browser renders the
            # JSON), so it is a faithful citation rather than an invented page.
            return f"https://ised-isde.canada.ca/cc/lgcy/api/corporations/{registry_id}.json"
        return None

    # trademarks_cipo_uspto: the registers expose search endpoints only, and the
    # stored registration_id may actually be an application/serial number, so
    # there is no permalink we can build without risking a dead link.
    return None


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
