from pathlib import Path

from app.services.uploads.pdf import count_pdf_pages

_SAMPLE_PDF = Path(__file__).parent.parent / "sandbox" / "cim" / "CIM 04.pdf"


def test_count_pdf_pages_reads_real_pdf():
    count = count_pdf_pages(_SAMPLE_PDF.read_bytes())
    assert isinstance(count, int) and 0 < count < 500


def test_count_pdf_pages_returns_none_for_garbage():
    assert count_pdf_pages(b"not a pdf") is None
