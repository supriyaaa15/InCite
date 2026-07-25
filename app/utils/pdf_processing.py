"""
PDF loading + chunking. Originally prototyped in scripts/test_ingestion.py
(Day 3-4) as a standalone script; promoted here once proven correct, so
both the real app (ingestion_service.py) and the test scripts share one
implementation instead of two copies drifting apart.
"""

import fitz  # PyMuPDF


def load_pages(pdf_path: str) -> list[tuple[int, str]]:
    """
    Returns [(page_number, page_text), ...]. Page numbers start at 1.

    Uses PyMuPDF (fitz) instead of pdfplumber — measured via the
    ingestion timing instrumentation (see ingestion_service.py) as the
    dominant bottleneck on larger PDFs: pdfplumber's PDF Loading stage
    alone was 44.48s of a 47.11s total on a 30-page document. PyMuPDF's
    C-based rendering engine is dramatically faster for plain text
    extraction, at the cost of less sophisticated layout/table detection
    than pdfplumber — an acceptable trade-off here since this pipeline
    only needs raw extractable text, not structured layout data.
    """
    pages = []
    with fitz.open(pdf_path) as pdf:
        for i, page in enumerate(pdf, start=1):
            text = (page.get_text() or "").replace("\x00", "") #prevents PostgreSQL from receiving NUL characters.
            if text.strip():
                pages.append((i, text))
    return pages


def chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Word-based overlapping chunks. See docs/design-decisions.md for the
    known limitation around small trailing chunks."""
    words = text.split()
    if not words:
        return []

    chunks = []
    start = 0
    while start < len(words):
        end = start + chunk_size
        chunks.append(" ".join(words[start:end]))
        if end >= len(words):
            break
        start = end - overlap

    return chunks