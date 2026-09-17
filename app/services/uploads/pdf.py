"""PDF page counting for the upload-complete page-count gate -- lets the
frontend reject an over-~110-page document at upload instead of ~2 minutes
into the analysis pipeline. pypdf is a direct dependency of this app for
exactly this narrow use (see pyproject.toml); the Docling-based parsing it
otherwise lived alongside was split out to Simpero_Gov_AI_Services.
"""

from __future__ import annotations

import io

from pypdf import PdfReader


def count_pdf_pages(data: bytes) -> int | None:
    """None on anything that isn't a cleanly readable PDF -- the caller
    returns that straight through as pageCount rather than failing the
    upload-complete request over a page count it can't determine.
    """
    try:
        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return None
