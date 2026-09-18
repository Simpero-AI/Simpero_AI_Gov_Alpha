"""PDF page counting for the upload-complete page-count gate -- lets the
frontend reject an over-~110-page document at upload instead of ~2 minutes
into the analysis pipeline. pypdf is a direct dependency of this app for
exactly this narrow use (see pyproject.toml); the Docling-based parsing it
otherwise lived alongside was split out to Simpero_Gov_AI_Services.
"""

from __future__ import annotations

import asyncio
import io
import logging

from pypdf import PdfReader

from app.services.uploads.spaces import get_object_bytes

logger = logging.getLogger(__name__)


def count_pdf_pages(data: bytes) -> int | None:
    """None on anything that isn't a cleanly readable PDF -- the caller
    returns that straight through as pageCount rather than failing the
    upload-complete request over a page count it can't determine.
    """
    try:
        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return None


def _fetch_and_count(storage_key: str, max_bytes: int) -> int | None:
    """Blocking fetch + parse -- only ever run off the event loop, via
    resolve_page_count. Broad except by design: this is a best-effort
    page count that must never fail the /complete request it's attached to,
    so a transient Spaces error (ClientError, timeout, ObjectTooLargeError,
    anything else) degrades to None exactly like an unparseable PDF does.
    """
    try:
        data = get_object_bytes(storage_key, max_bytes)
    except Exception:
        logger.warning("page-count fetch failed for %r", storage_key, exc_info=True)
        return None
    return count_pdf_pages(data)


async def resolve_page_count(filename: str, storage_key: str, max_bytes: int) -> int | None:
    """Best-effort page count for a just-completed upload. None immediately
    for a non-PDF filename; otherwise the blocking Spaces read (up to
    max_bytes) and pypdf parse both run in a thread via asyncio.to_thread, so
    a large document's I/O + CPU-bound parse never stalls the event loop for
    other tenants' concurrent requests. Never raises -- shared by both the
    authenticated and public intake /complete routes so this contract only
    has to be right in one place.
    """
    if not filename.lower().endswith(".pdf"):
        return None
    return await asyncio.to_thread(_fetch_and_count, storage_key, max_bytes)
