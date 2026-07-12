"""
PDF loading + chunking. Originally prototyped in scripts/test_ingestion.py
(Day 3-4) as a standalone script; promoted here once proven correct, so
both the real app (ingestion_service.py) and the test scripts share one
implementation instead of two copies drifting apart.
"""

import pdfplumber


def load_pages(pdf_path: str) -> list[tuple[int, str]]:
    """Returns [(page_number, page_text), ...]. Page numbers start at 1."""
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
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
