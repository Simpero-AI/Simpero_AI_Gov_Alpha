import io

from pypdf import PdfWriter

from app.services.uploads.pdf import count_pdf_pages


def _pdf_bytes(num_pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_count_pdf_pages_reads_real_pdf():
    assert count_pdf_pages(_pdf_bytes(3)) == 3


def test_count_pdf_pages_returns_none_for_garbage():
    assert count_pdf_pages(b"not a pdf") is None
