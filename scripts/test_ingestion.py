"""
Day 3-4: manual PDF loading + chunking, no FastAPI, no database.

Run it against any PDF to see exactly what chunks get produced and confirm
page numbers + chunk indices are correct before this logic ever touches
the app.

Usage (from inside the running app container, since that's where
pdfplumber is installed):

    docker compose exec app python scripts/test_ingestion.py samples/notes.pdf

Drop any PDF anywhere inside the project folder first (e.g. into
samples/) — the whole project is mounted into the container, so the
container can see it immediately without rebuilding.
"""

import sys

from pathlib import Path

# Running this file directly only puts scripts/ on sys.path, not the project
# root — so `from app.core.config import settings` fails without this line.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pdfplumber

from app.core.config import settings


def load_pages(pdf_path: str) -> list[tuple[int, str]]:
    """
    Returns a list of (page_number, page_text) tuples.
    Page numbers start at 1, matching what a human would call "page 1" —
    this is what gets stored in the chunks.page_number column later, so
    citations point to the same page number a person would expect.
    """
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            if text.strip():
                pages.append((i, text))
    return pages


def chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """
    Splits text into overlapping chunks, word-based for readability.

    chunk_size: how many words per chunk.
    overlap: how many words from the end of one chunk repeat at the start
    of the next. This is what preserves context across a chunk boundary —
    without it, a sentence split across two chunks loses meaning in both.
    """
    words = text.split()
    if not words:
        return []

    chunks = []
    start = 0
    while start < len(words):
        end = start + chunk_size
        chunk_words = words[start:end]
        chunks.append(" ".join(chunk_words))
        if end >= len(words):
            break
        start = end - overlap  # step back by `overlap` words before the next chunk

    return chunks


def main(pdf_path: str) -> None:
    print(f"Loading: {pdf_path}")
    print(f"Config -> CHUNK_SIZE={settings.CHUNK_SIZE}, CHUNK_OVERLAP={settings.CHUNK_OVERLAP}\n")

    pages = load_pages(pdf_path)
    print(f"Extracted text from {len(pages)} page(s) with content.\n")

    total_chunks = 0
    for page_number, page_text in pages:
        page_chunks = chunk_text(page_text, settings.CHUNK_SIZE, settings.CHUNK_OVERLAP)

        for chunk_index, chunk in enumerate(page_chunks):
            total_chunks += 1
            preview = chunk[:120].replace("\n", " ")
            print(f"[page {page_number}, chunk {chunk_index}] ({len(chunk.split())} words)")
            print(f"  {preview}...\n")

    print(f"Total chunks produced: {total_chunks}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/test_ingestion.py <path_to_pdf>")
        sys.exit(1)

    main(sys.argv[1])