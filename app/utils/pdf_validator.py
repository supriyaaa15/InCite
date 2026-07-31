"""
PDF validation helpers for the upload pipeline.

Design rationale
----------------
Two-phase approach, matching the route handler order:

1.  validate_upload(file)   — cheap, runs BEFORE saving to disk.
    Checks file size.  MIME type and extension are inspected as hints to give
    faster, friendlier feedback, but they are NOT treated as authoritative:
    a renamed .exe will still reach fitz and fail there.

2.  validate_pdf_file(path) — authoritative, runs AFTER saving to disk.
    Uses fitz (PyMuPDF) as the single source of truth for "is this really a PDF?"
    Checks: openable, not encrypted, not empty, page count within limit.
    Reports NO_EXTRACTABLE_TEXT when every page is image-only — but does NOT
    reject the file as invalid; the message explains OCR isn't supported.

Both functions raise UploadValidationError on failure.  The route handler
catches that exception, deletes the saved file, and returns HTTP 422.
"""

import logging

import fitz  # PyMuPDF

from app.core.config import settings
from app.utils.upload_errors import UploadErrorCode, UploadValidationError

logger = logging.getLogger(__name__)

# These are hint-only — used for early user feedback, not authoritative.
_PDF_MIME_TYPES = {"application/pdf"}
_PDF_EXTENSIONS = {".pdf"}


def validate_upload(file) -> None:
    """
    Cheap pre-save checks.  Raises UploadValidationError early so we avoid
    writing obviously bad data to disk.

    Parameters
    ----------
    file : UploadFile
        The FastAPI upload file object (file.file pointer is still at position 0).
    """
    # --- Size check (authoritative) -------------------------------------------
    # Read the entire stream to count bytes, then seek back so save_file() can
    # re-read it.  We do this before writing to disk so we don't waste I/O on
    # oversized files.
    file.file.seek(0, 2)          # seek to end
    size_bytes = file.file.tell()
    file.file.seek(0)             # rewind for the actual save

    if size_bytes > settings.max_upload_size_bytes:
        limit_mb = settings.MAX_UPLOAD_SIZE_MB
        raise UploadValidationError(
            UploadErrorCode.FILE_TOO_LARGE,
            f"This file exceeds the maximum upload size of {limit_mb} MB. "
            f"Please upload a smaller PDF.",
        )

    # --- MIME / extension hints (non-authoritative) ---------------------------
    # These catch the obvious case (user picks the wrong file type by accident)
    # and give instant feedback.  They are NOT the final validation gate —
    # fitz.open() in validate_pdf_file() is.
    content_type = (file.content_type or "").lower().split(";")[0].strip()
    filename = file.filename or ""
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    hint_fail = content_type not in _PDF_MIME_TYPES and ext not in _PDF_EXTENSIONS
    if hint_fail:
        raise UploadValidationError(UploadErrorCode.INVALID_FILE)


def validate_pdf_file(file_path: str) -> None:
    """
    Authoritative PDF validation.  Must be called AFTER the file has been
    written to disk.

    Checks (in order of cheapness):
    1. fitz can open the file at all   → INVALID_FILE / INVALID_PDF
    2. Not encrypted                   → PASSWORD_PROTECTED
    3. Has at least one page           → EMPTY_DOCUMENT
    4. Page count within limit         → PAGE_LIMIT_EXCEEDED
    5. Has at least one char of text   → NO_EXTRACTABLE_TEXT  (not a hard reject)

    Parameters
    ----------
    file_path : str
        Absolute path to the saved file.

    Raises
    ------
    UploadValidationError
        With the appropriate UploadErrorCode if any check fails.
    """
    try:
        pdf = fitz.open(file_path)
    except fitz.FileDataError:
        # fitz recognised a PDF header but couldn't parse the body
        raise UploadValidationError(UploadErrorCode.INVALID_PDF)
    except Exception:
        # fitz couldn't even identify the format — not a PDF at all
        raise UploadValidationError(UploadErrorCode.INVALID_FILE)

    with pdf:
        # Encrypted check must come first — accessing pages on a locked doc
        # raises its own exception rather than returning useful data.
        if pdf.is_encrypted:
            raise UploadValidationError(UploadErrorCode.PASSWORD_PROTECTED)

        page_count = pdf.page_count
        if page_count == 0:
            raise UploadValidationError(UploadErrorCode.EMPTY_DOCUMENT)

        if page_count > settings.MAX_PDF_PAGES:
            limit = settings.MAX_PDF_PAGES
            raise UploadValidationError(
                UploadErrorCode.PAGE_LIMIT_EXCEEDED,
                f"This PDF has {page_count} pages, which exceeds the maximum "
                f"of {limit} pages. Please upload a shorter document.",
            )

        # Count total extractable characters across all pages.
        # We sample every page but short-circuit as soon as we find any text —
        # the common case for valid documents is fast.
        total_chars = 0
        for page in pdf:
            text = page.get_text() or ""
            total_chars += len(text.strip())
            if total_chars > 0:
                break  # found text — no need to check further

        if total_chars == 0:
            # Valid PDF, but the pipeline cannot extract text from it.
            # This is an OCR limitation, not a corrupt file — so we raise
            # with a specific code and a friendly explanation.
            raise UploadValidationError(UploadErrorCode.NO_EXTRACTABLE_TEXT)
